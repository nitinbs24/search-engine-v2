import httpx


class ModelClient:
    def __init__(self, base_url: str):
        self._http = httpx.Client(base_url=base_url, timeout=30)

    def embed_query(self, text: str) -> list[float]:
        r = self._http.post("/embed/query", json={"query": text})
        r.raise_for_status()
        return r.json()["text_vector"]
