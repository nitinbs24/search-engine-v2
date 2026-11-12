import httpx


class ModelClient:
    def __init__(self, base_url: str, transport=None):
        self._http = httpx.Client(base_url=base_url, timeout=30, transport=transport)

    def embed_query(self, text: str) -> dict:
        """Returns {"text_vector": ..., "image_vector": ...}."""
        r = self._http.post("/embed/query", json={"query": text})
        r.raise_for_status()
        return r.json()

    def rerank(self, query: str, passages: list[str], highlight_top: int) -> dict:
        """Returns {"scores": ..., "highlights": ...}."""
        r = self._http.post("/rerank", json={"query": query, "passages": passages, "highlight_top": highlight_top})
        r.raise_for_status()
        return r.json()

    def answer(self, question: str, sources: list[dict]) -> str | None:
        """None when the model server has no answer model."""
        r = self._http.post("/answer", json={"question": question, "sources": sources}, timeout=120)
        if r.status_code == 503:
            return None
        r.raise_for_status()
        return r.json()["answer"]
