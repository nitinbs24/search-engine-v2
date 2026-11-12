import asyncio

import httpx
import pytest

from chunker import TextChunker
from main import IngestionWorker
from model_client import ModelClient
from tika_extractor import ExtractionResult

TEXT = "The quarterly report shows revenue growth in every region. " * 40


def created_event(key="reports/q1.txt", size=2400):
    return {"Records": [{
        "eventName": "s3:ObjectCreated:Put",
        "eventTime": "2026-03-04T05:06:07.000Z",
        "s3": {"bucket": {"name": "uploads"},
               "object": {"key": key.replace("/", "%2F"), "size": size, "contentType": "text/plain"}},
    }]}


class FakeMinio:
    def get_object(self, bucket, key):
        return httpx.Response(200, content=TEXT.encode())  # has .read() and .close()


class FakeTika:
    def __init__(self, result=None):
        self.result = result or ExtractionResult(text=TEXT, metadata={"dc:title": "Q1 report"})

    async def extract(self, file_bytes, content_type):
        return self.result


class FakeModel:
    async def embed_texts(self, texts):
        return [[0.5, 0.5] for _ in texts]


class FakeIndex:
    def __init__(self):
        self.docs, self.deleted = [], []

    def bulk_index(self, docs):
        self.docs.extend(docs)

    def delete_by_object_key(self, bucket, key):
        self.deleted.append((bucket, key))


def worker(index, tika=None):
    return IngestionWorker(FakeMinio(), tika or FakeTika(), TextChunker(), FakeModel(), index)


def test_text_upload_is_indexed_as_chunks_with_embeddings():
    index = FakeIndex()
    asyncio.run(worker(index).process_event(created_event()))

    assert len(index.docs) > 1
    assert [d["chunk_index"] for d in index.docs] == list(range(len(index.docs)))
    first = index.docs[0]
    assert first["text_embedding"] == [0.5, 0.5]
    assert first["chunk_total"] == len(index.docs)
    assert (first["bucket"], first["object_key"], first["filename"], first["extension"]) == (
        "uploads", "reports/q1.txt", "q1.txt", "txt")
    assert (first["mime_type"], first["size_bytes"], first["title"]) == ("text/plain", 2400, "Q1 report")


def test_uploaded_at_is_the_event_time_not_the_processing_time():
    index = FakeIndex()
    asyncio.run(worker(index).process_event(created_event()))
    assert {d["uploaded_at"] for d in index.docs} == {"2026-03-04T05:06:07.000Z"}


def test_chunk_ids_are_stable_per_object_and_chunk():
    index = FakeIndex()
    asyncio.run(worker(index).process_event(created_event()))
    assert index.docs[1]["_id"] == "uploads/reports/q1.txt#1"


def test_removed_object_is_deleted_from_the_index():
    index = FakeIndex()
    event = created_event()
    event["Records"][0]["eventName"] = "s3:ObjectRemoved:Delete"
    asyncio.run(worker(index).process_event(event))
    assert index.deleted == [("uploads", "reports/q1.txt")] and index.docs == []


def test_failed_extraction_raises_so_the_message_reaches_the_dlq():
    tika = FakeTika(ExtractionResult(text="", success=False, error="Tika extraction failed"))
    with pytest.raises(RuntimeError, match="Tika extraction failed"):
        asyncio.run(worker(FakeIndex(), tika).process_event(created_event()))


def test_model_client_sends_texts_in_batches_and_keeps_order():
    batches = []

    def handler(request):
        texts = __import__("json").loads(request.content)["texts"]
        batches.append(len(texts))
        return httpx.Response(200, json={"embeddings": [[float(t)] for t in texts]})

    client = ModelClient("http://model-server:8000", transport=httpx.MockTransport(handler))
    vectors = asyncio.run(client.embed_texts([str(i) for i in range(150)]))

    assert batches == [64, 64, 22]
    assert vectors == [[float(i)] for i in range(150)]


def test_model_client_raises_on_server_error():
    client = ModelClient("http://x", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.embed_texts(["a"]))
