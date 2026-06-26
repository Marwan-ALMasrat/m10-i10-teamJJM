"""RAG composer — retrieve → assemble → generate → cite → grounding check.

Grounding contract: when `answer` is not the empty-retrieval sentinel,
`len(citations) > 0` is required. Every cited `chunk_id` corresponds to
a chunk in the top-`k` retrieved from Weaviate.

Generator called with `do_sample=False` for reproducibility.

"""
import json
import logging
import re
import threading
from typing import Tuple

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """\
You are answering a recipe question. Use ONLY the numbered sources below.
Cite each claim with the source number in square brackets, e.g. [1].
If the sources do not contain the answer, say: I cannot answer this from the available sources.

Sources:
{sources}

Question: {question}
Answer:"""

SENTINEL = "I cannot answer this from the available sources"
CITATION_PATTERN = re.compile(r"\[(\d+)\]")
GENERATOR_TIMEOUT = 60  # seconds


# ─────────────────────────────────────────────
# 1. التحقق من البيانات  (من النسخة الأولى)
# ─────────────────────────────────────────────

def parse_retrieved_chunks(raw_query) -> list[dict] | None:
    """Normalize Weaviate's response; return None for invalid shapes."""
    if not isinstance(raw_query, dict):
        return None
    data = raw_query.get("data")
    if not isinstance(data, dict):
        return None
    get_block = data.get("Get")
    if not isinstance(get_block, dict):
        return None
    chunks = get_block.get("Chunk")
    if not isinstance(chunks, list):
        return None

    retrieved = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            return None
        additional = chunk.get("_additional")
        if not isinstance(additional, dict):
            return None
        distance = additional.get("distance")
        if "chunk_id" not in chunk or "text" not in chunk or distance is None:
            return None
        try:
            score = 1.0 - float(distance)
        except (TypeError, ValueError):
            return None
        retrieved.append(
            {
                "chunk_id": int(chunk["chunk_id"]),
                "text": chunk["text"],
                "score": score,
            }
        )
    return retrieved


# ─────────────────────────────────────────────
# 2. Prompt assembly
# ─────────────────────────────────────────────

def assemble_prompt(question: str, chunks: list[dict]) -> Tuple[str, dict[int, dict]]:
    """Number the retrieved chunks 1..k and substitute into the prompt template.

    Returns (prompt_str, {citation_index: chunk_dict}). Index starts at 1.
    """
    numbered: dict[int, dict] = {}
    lines = []
    for i, chunk in enumerate(chunks, start=1):
        numbered[i] = chunk
        lines.append(f"[{i}] {chunk['text']}")
    sources = "\n".join(lines)
    return PROMPT_TEMPLATE.format(sources=sources, question=question), numbered


# ─────────────────────────────────────────────
# 3. Citation extraction
# ─────────────────────────────────────────────

def extract_citations(answer: str, numbered: dict[int, dict]) -> list[dict]:
    """Pull [N]-style markers from `answer` and resolve to retrieved chunks.

    Returns one {"chunk_id", "score"} dict per unique resolvable index.
    """
    cited: list[dict] = []
    seen: set[int] = set()
    for match in CITATION_PATTERN.finditer(answer):
        idx = int(match.group(1))
        if idx in numbered and idx not in seen:
            seen.add(idx)
            chunk = numbered[idx]
            cited.append({"chunk_id": int(chunk["chunk_id"]), "score": chunk["score"]})
    return cited


# ─────────────────────────────────────────────
# 4. Fallback ذكي  (من النسخة الأخيرة)
# ─────────────────────────────────────────────

def _is_answer_too_short(answer: str) -> bool:
    """Return True if the answer is too short to be useful.

    flan-t5-base sometimes returns only '[1].' or a very short string.
    In that case we fall back to the cited chunk text.
    """
    stripped = CITATION_PATTERN.sub("", answer).strip().rstrip(".")
    return len(stripped) < 10


def _build_fallback_answer(citations: list[dict], numbered: dict[int, dict]) -> str:
    """Build an answer from the cited chunk texts when flan-t5 is too short.

    Keeps the [N] citation markers so the frontend can still render them.
    """
    parts = []
    # Build a lookup: chunk_id -> (idx, chunk) for O(n) resolution
    chunk_id_to_idx = {
        int(chunk["chunk_id"]): (idx, chunk)
        for idx, chunk in numbered.items()
    }
    for c in citations:
        entry = chunk_id_to_idx.get(c["chunk_id"])
        if entry:
            idx, chunk = entry
            parts.append(f"{chunk['text']} [{idx}]")
    return " ".join(parts)


# ─────────────────────────────────────────────
# 5. Timeout  (من النسخة الثانية)
# ─────────────────────────────────────────────

def _run_generator_with_timeout(generator, prompt: str, timeout: int) -> str:
    """Run the generator in a thread with a timeout.

    Returns the generated text or raises TimeoutError if the generator
    does not complete within `timeout` seconds.
    """
    result: list[str] = []
    error: list[Exception] = []

    def _target():
        try:
            output = generator(prompt, max_new_tokens=256, do_sample=False)
            result.append(output[0]["generated_text"])
        except Exception as e:
            error.append(e)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        raise TimeoutError(f"Generator did not complete within {timeout}s")
    if error:
        raise error[0]
    return result[0]


# ─────────────────────────────────────────────
# 6. Main pipeline
# ─────────────────────────────────────────────

def compose_rag(question: str, embedder, weaviate_client, generator, k: int = 4) -> dict:
    """Run the four-stage RAG pipeline.

    Encodes the question via the externally-loaded sentence-transformers
    embedder and queries Weaviate with `with_near_vector`. The Weaviate
    class is `vectorizer=none`, so `with_near_text` would fail at
    runtime with `KeyError: 'data'`.

    Returns {"answer": str, "citations": list[dict], "confidence": float}.
    """
    # 1. Retrieve
    vector = embedder.encode(question).tolist()
    raw_query = (
        weaviate_client.query.get("Chunk", ["chunk_id", "text"])
        .with_near_vector({"vector": vector})
        .with_limit(k)
        .with_additional(["distance"])
        .do()
    )

    # ✅ التحقق من البيانات بأمان
    retrieved = parse_retrieved_chunks(raw_query)

    logger.info(json.dumps({
        "event": "rag_retrieve",
        "question": question,
        "k": k,
        "chunks_retrieved": len(retrieved) if retrieved else 0,
    }))

    if not retrieved:
        logger.info(json.dumps({
            "event": "rag_sentinel",
            "reason": "empty_retrieval",
            "question": question,
        }))
        return {"answer": SENTINEL, "citations": [], "confidence": 0.0}

    # 2. Assemble prompt
    prompt, numbered = assemble_prompt(question, retrieved)

    # 3. Generate with timeout ✅
    try:
        raw = _run_generator_with_timeout(generator, prompt, timeout=GENERATOR_TIMEOUT)
    except TimeoutError:
        logger.error(json.dumps({
            "event": "rag_generator_timeout",
            "question": question,
            "timeout_seconds": GENERATOR_TIMEOUT,
        }))
        return {"answer": SENTINEL, "citations": [], "confidence": 0.0}

    # 4. Extract citations
    citations = extract_citations(raw, numbered)

    if not citations:
        logger.info(json.dumps({
            "event": "rag_sentinel",
            "reason": "no_citations",
            "question": question,
        }))
        return {"answer": SENTINEL, "citations": [], "confidence": 0.0}

    confidence = sum(c["score"] for c in citations) / len(citations)
    confidence = max(0.0, min(1.0, confidence))

    # 5. Fallback ذكي — إذا كانت الإجابة قصيرة جداً ✅
    if _is_answer_too_short(raw):
        logger.info(json.dumps({
            "event": "rag_fallback",
            "reason": "answer_too_short",
            "raw": raw,
        }))
        answer = _build_fallback_answer(citations, numbered)
    else:
        answer = raw

    logger.info(json.dumps({
        "event": "rag_answer",
        "question": question,
        "citations_count": len(citations),
        "confidence": round(confidence, 3),
    }))

    return {"answer": answer, "citations": citations, "confidence": confidence}