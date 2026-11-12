from opensearchpy import OpenSearch, helpers


class Index:
    def __init__(self, url: str, index: str):
        self.index = index
        self._client = OpenSearch(hosts=[url], timeout=30, max_retries=3, retry_on_timeout=True,
                                    # the default omits 429; 4xx other than 429 are permanent and never retried
                                    retry_on_status=(429, 502, 503, 504))

    def bulk_index(self, docs: list[dict]) -> None:
        """Index chunks under their stable ids. Raises if any chunk fails."""
        actions = [
            {"_index": self.index, "_id": d["_id"], "_source": {k: v for k, v in d.items() if k != "_id"}}
            for d in docs
        ]
        # wait_for: a delete arriving right after must be able to see these chunks
        # A throttled item (status 429 inside an HTTP 200 reply) is retried. The client only does
        # that when it is not raising on the first failed item, so the raise is done here.
        _, errors = helpers.bulk(self._client, actions, chunk_size=200, refresh="wait_for",
                                 max_retries=3, raise_on_error=False)
        if errors:
            raise helpers.BulkIndexError(f"{len(errors)} document(s) failed to index.", errors)

    def delete_by_object_key(self, bucket: str, key: str, keep_ids=()) -> None:
        """Delete the object's chunks, except those whose ids are in keep_ids."""
        query = {"bool": {"filter": [{"term": {"bucket": bucket}}, {"term": {"object_key": key}}]}}
        if keep_ids:
            query["bool"]["must_not"] = [{"ids": {"values": list(keep_ids)}}]
        self._client.delete_by_query(index=self.index, body={"query": query}, refresh=True, conflicts="proceed")
