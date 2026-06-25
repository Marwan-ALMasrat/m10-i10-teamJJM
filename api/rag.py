"""RAG composer — retrieve → assemble → generate → cite → grounding check.

Grounding contract: when `answer` is not the empty-retrieval sentinel,
`len(citations) > 0` is required. Every cited `chunk_id` corresponds to
a chunk in the top-`k` retrieved from Weaviate.

Generator called with `do_sample=False` for reproducibility.
"""
import re
import logging
import concurrent.futures
from typing import Tuple

logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "event": "%(message)s"}'
)
logger = logging.getLogger("rag_service")

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
            cited.append({"chunk_id": chunk["chunk_id"], "score": chunk["score"]})
    return cited


def compose_rag(question: str, embedder, weaviate_client, generator, k: int = 4) -> dict:
    """Run the four-stage RAG pipeline.

    Encodes the question via the externally-loaded sentence-transformers
    embedder and queries Weaviate with `with_near_vector`. The Weaviate
    class is `vectorizer=none`, so `with_near_text` would fail at
    runtime with `KeyError: 'data'`.

    Returns {"answer": str, "citations": list[dict], "confidence": float}.
    """
    logger.info(f"Received RAG request for question: '{question}' with k={k}")
    
    try:
        vector = embedder.encode(question).tolist()
        raw_query = (
            weaviate_client.query.get("Chunk", ["chunk_id", "text"])
            .with_near_vector({"vector": vector})
            .with_limit(k)
            .with_additional(["distance"])
            .do()
        )
        retrieved = [
            {
                "chunk_id": c["chunk_id"],
                "text": c["text"],
                "score": 1.0 - c["_additional"]["distance"],
            }
            for c in raw_query["data"]["Get"]["Chunk"]
        ]
    except Exception as e:
        logger.error(f"Database retrieval failed: {str(e)}")
        raise e

    if not retrieved:
        logger.warning(f"No chunks retrieved for question: '{question}'")
        return {"answer": SENTINEL, "citations": [], "confidence": 0.0}

    prompt, numbered = assemble_prompt(question, retrieved)
    
    # 2. Timeout implementation (60 Seconds)
    logger.info("Submitting prompt to the generator model...")
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(generator, prompt, max_new_tokens=256, do_sample=False)
        
            raw_generation = future.result(timeout=60.0) 
            raw = raw_generation[0]["generated_text"]
            logger.info("Model generation completed successfully.")
    except concurrent.futures.TimeoutError:
        logger.error(f"Generation timed out after 60 seconds for question: '{question}'")
    
        raise TimeoutError("The generation model took too long to respond.")
    except Exception as e:
        logger.error(f"Model generation failed: {str(e)}")
        raise e

    citations = extract_citations(raw, numbered)
    if not citations:
        logger.warning("Generation yielded no verifiable citations. Returning sentinel.")
        return {"answer": SENTINEL, "citations": [], "confidence": 0.0}

    confidence = sum(c["score"] for c in citations) / len(citations)
    confidence = max(0.0, min(1.0, confidence))
    
    logger.info(f"RAG process finished successfully. Confidence: {confidence:.2f}")
    return {"answer": raw, "citations": citations, "confidence": confidence}