#!/usr/bin/env bash
# Seed the running Neo4j container with the recipe fixture.
#
# Idempotent — MERGE and CREATE CONSTRAINT IF NOT EXISTS in seed.cypher
# mean repeat runs do not duplicate nodes.
#
# Run from the repo root (the directory containing docker-compose.yml):
#   bash scripts/seed_neo4j.sh

set -euo pipefail

NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:?NEO4J_PASSWORD is required}"

echo "Seeding Neo4j with recipe fixture..."

docker compose exec -T neo4j \
  cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
  < api/seed.cypher

echo "Neo4j seeded successfully."