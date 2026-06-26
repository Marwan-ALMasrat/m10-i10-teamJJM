#!/usr/bin/env bash
# Seed the running Weaviate container with the chunked-docs fixture.
#
# Idempotent — the Python seeder skips chunk_ids already present.
#
# Runs the seed INSIDE the api container so that sentence-transformers,
# weaviate-client, and all api requirements are available.
#
# Run from the repo root (the directory containing docker-compose.yml):
#   bash scripts/seed_weaviate.sh

set -euo pipefail

WEAVIATE_URL="${WEAVIATE_URL:-http://weaviate:8080}"

echo "Seeding Weaviate with chunked-docs fixture..."

docker compose exec -T \
  -e WEAVIATE_URL="$WEAVIATE_URL" \
  api \
  python api/seed_weaviate.py

echo "Weaviate seeded successfully."