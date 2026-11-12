import httpx

BATCH = 64  # the model server accepts at most 256 texts per request


class ModelClient:
    def __init__(self, base_url: str, transport=None):
        self._http = httpx.AsyncClient(base_url=base_url, timeout=120, transport=transport)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            r = await self._http.post("/embed/text", json={"texts": texts[i:i + BATCH]})
            r.raise_for_status()
            vectors.extend(r.json()["embeddings"])
        return vectors
