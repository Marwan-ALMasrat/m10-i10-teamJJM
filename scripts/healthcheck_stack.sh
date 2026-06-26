#!/usr/bin/env bash
# Poll docker compose ps until all four services report healthy
# or until the 90s budget expires.
#
# Run from the repo root (the directory containing docker-compose.yml):
#   bash scripts/healthcheck_stack.sh

set -euo pipefail

SERVICES=("neo4j" "weaviate" "api" "web")
MAX_ITERATIONS=45
SLEEP_SECONDS=2

echo "Waiting for all services to become healthy (max ${MAX_ITERATIONS}x${SLEEP_SECONDS}s)..."

for ((i = 1; i <= MAX_ITERATIONS; i++)); do
  all_healthy=true

  for service in "${SERVICES[@]}"; do
    # Extract Health field for this service from JSON output
    health=$(docker compose ps --format json "$service" 2>/dev/null \
      | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
        print(obj.get('Health', obj.get('Status', 'unknown')))
    except json.JSONDecodeError:
        print('unknown')
" 2>/dev/null || echo "unknown")

    if [[ "$health" != "healthy" ]]; then
      all_healthy=false
      echo "  [$i/$MAX_ITERATIONS] $service: $health"
    fi
  done

  if $all_healthy; then
    echo "All four services are healthy."
    exit 0
  fi

  sleep "$SLEEP_SECONDS"
done

echo "Timeout: not all services became healthy within $((MAX_ITERATIONS * SLEEP_SECONDS))s."
docker compose ps
exit 1