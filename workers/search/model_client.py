import httpx


class ModelClient:
    def __init__(self, base_url: str, transport=None):
        self._http = httpx.Client(base_url=base_url, timeout=30, transport=transport)

    def embed_query(self, text: str) -> dict:
        """Returns {"text_vector": ..., "image_vector": ...}."""
        r = self._http.post("/embed/query", json={"query": text})
        r.raise_for_status()
        return r.json()

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        r = self._http.post("/rerank", json={"query": query, "passages": passages})
        r.raise_for_status()
        return r.json()["scores"]
