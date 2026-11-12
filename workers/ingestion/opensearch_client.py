from opensearchpy import OpenSearch, helpers


class Index:
    def __init__(self, url: str, index: str):
        self.index = index
        self._client = OpenSearch(hosts=[url], timeout=30, max_retries=3, retry_on_timeout=True)

    def bulk_index(self, docs: list[dict]) -> None:
        """Index chunks under their stable ids. Raises if any chunk fails."""
        actions = [
            {"_index": self.index, "_id": d["_id"], "_source": {k: v for k, v in d.items() if k != "_id"}}
            for d in docs
        ]
        # wait_for: a delete arriving right after must be able to see these chunks
        helpers.bulk(self._client, actions, chunk_size=200, refresh="wait_for")

    def delete_by_object_key(self, bucket: str, key: str) -> None:
        query = {"bool": {"filter": [{"term": {"bucket": bucket}}, {"term": {"object_key": key}}]}}
        self._client.delete_by_query(index=self.index, body={"query": query}, refresh=True, conflicts="proceed")
