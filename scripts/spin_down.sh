#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-safe}"

if [[ "$MODE" == "safe" ]]; then
  docker compose down
  exit 0
fi

if [[ "$MODE" == "deep" ]]; then
  docker compose down --volumes --remove-orphans
  exit 0
fi

echo "Usage: $0 [safe|deep]"
exit 1
