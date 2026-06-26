"""End-to-end smoke harness — Infra-Integration lead authors.

Brings the four-service stack up via `docker compose up -d --wait` and
verifies the demo `/rag/answer` curl returns 200 with citations against
the seeded fixture. Skipped in the autograder (which exercises compose
topology structurally, not at runtime); used locally during demo-prep
and by the TA during walkthrough.
"""
import os
import time

import pytest
import requests

API_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
DEMO_QUESTION = "How do I prep ginger for stir-fry?"


def wait_for_healthy(url: str, timeout: int = 300, interval: int = 5) -> bool:
    """Poll url until 200 or timeout (seconds)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(interval)
    return False


def test_stack_e2e_seeded_rag_query():
    """POST /rag/answer with the seeded question returns 200 with citations."""
    # Wait for the api to be healthy before exercising it
    healthy = wait_for_healthy(f"{API_BASE}/healthz", timeout=300)
    assert healthy, f"API did not become healthy within 300s at {API_BASE}/healthz"

    # Submit the demo question
    response = requests.post(
        f"{API_BASE}/rag/answer",
        json={"question": DEMO_QUESTION, "k": 4},
        timeout=120,
    )

    assert response.status_code == 200, (
        f"Expected 200, got {response.status_code}: {response.text}"
    )

    body = response.json()

    # Grounding contract: citations must be present and answer not sentinel
    assert len(body["citations"]) > 0, "Expected at least one citation"
    assert body["confidence"] > 0, "Expected confidence > 0"
    assert body["answer"] != "I cannot answer this from the available sources", (
        "Got sentinel response — check that Weaviate is seeded"
    )

    # Every cited chunk_id must be a positive integer
    for citation in body["citations"]:
        assert isinstance(citation["chunk_id"], int) and citation["chunk_id"] > 0
        assert 0.0 <= citation["score"] <= 1.0


def test_healthz_returns_ok():
    """GET /healthz returns 200 with status ok."""
    healthy = wait_for_healthy(f"{API_BASE}/healthz", timeout=60)
    assert healthy, "API /healthz did not return 200"

    response = requests.get(f"{API_BASE}/healthz", timeout=10)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_returns_ok():
    """/readyz returns 200 only when Neo4j and Weaviate are both healthy."""
    response = requests.get(f"{API_BASE}/readyz", timeout=10)
    assert response.status_code == 200, (
        f"Expected 200, got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert body["neo4j"] == "ok", f"Neo4j not ready: {body}"
    assert body["weaviate"] == "ok", f"Weaviate not ready: {body}"