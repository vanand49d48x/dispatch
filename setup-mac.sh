#!/usr/bin/env bash
# One-time laptop setup for Dispatch on macOS. Run from the dispatch folder:  ./setup-mac.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/4 Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Desktop not found. Install it from https://www.docker.com/products/docker-desktop/ then rerun."
  exit 1
fi
docker info >/dev/null 2>&1 || { echo "Start Docker Desktop, wait for it to say 'running', then rerun."; exit 1; }
mem_gb=$(( $(docker info --format '{{.MemTotal}}') / 1024 / 1024 / 1024 ))
[ "$mem_gb" -lt 7 ] && echo "⚠️  Docker has ${mem_gb} GB RAM. Raise it to 8 GB+ in Docker Desktop → Settings → Resources."

echo "== 2/4 Config"
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "Created config.yaml - fill in your keys, save, then rerun this script."
  open -e config.yaml 2>/dev/null || true
  exit 0
fi
mkdir -p data

echo "== 3/4 Build + preflight (config, every account, permissions, limits)"
docker compose build
if ! docker compose run --rm dispatch python -m harness --config /app/host/config.yaml --check; then
  echo
  echo "✘ Preflight failed - nothing was started. Fix the ✘ items in config.yaml, then rerun ./setup-mac.sh"
  exit 1
fi

echo "== 4/4 Start (auto-restarts with Docker)"
docker compose up -d
echo
echo "✅ Running. Logs: docker compose logs -f    Stop: docker compose down"
echo
echo "Keep the Mac awake (plugged in), once:"
echo "  sudo pmset -a sleep 0 disksleep 0 standby 0 powernap 0 && sudo pmset -a disablesleep 1"
echo "Also: Docker Desktop → Settings → General → 'Start Docker Desktop when you sign in'."
