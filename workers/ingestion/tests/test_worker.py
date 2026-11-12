import asyncio
import io

import httpx
import pytest
from PIL import Image

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
    def __init__(self, data=TEXT.encode()):
        self.data = data

    def get_object(self, bucket, key):
        return httpx.Response(200, content=self.data)  # has .read() and .close()


class FakeTika:
    def __init__(self, result=None):
        self.result = result or ExtractionResult(text=TEXT, metadata={"dc:title": "Q1 report"})

    async def extract(self, file_bytes, content_type):
        return self.result


class FakeModel:
    def __init__(self):
        self.images = []

    async def embed_texts(self, texts):
        return [[0.5, 0.5] for _ in texts]

    async def embed_image(self, data):
        self.images.append(data)
        return [0.1, 0.2]


class FakeIndex:
    def __init__(self):
        self.docs, self.deleted = [], []

    def bulk_index(self, docs):
        self.docs.extend(docs)

    def delete_by_object_key(self, bucket, key):
        self.deleted.append((bucket, key))


def worker(index, tika=None, minio=None, model=None):
    return IngestionWorker(minio or FakeMinio(), tika or FakeTika(), TextChunker(), model or FakeModel(), index)


def jpeg(size=(40, 20), exif=True):
    image = Image.new("RGB", size, "red")
    tags = Image.Exif()
    if exif:
        tags[271], tags[272] = "Canon", "EOS R5"
        tags[0x8769] = {36867: "2024:03:15 14:22:05"}
    buf = io.BytesIO()
    image.save(buf, "JPEG", exif=tags)
    return buf.getvalue()


def image_event(key="photos/beach.jpg"):
    event = created_event(key)
    event["Records"][0]["s3"]["object"]["contentType"] = "image/jpeg"
    return event


def test_jpeg_upload_is_one_document_with_vector_and_filename_exif_text():
    index, model = FakeIndex(), FakeModel()
    asyncio.run(worker(index, minio=FakeMinio(jpeg()), model=model).process_event(image_event()))

    (doc,) = index.docs
    assert doc["image_embedding"] == [0.1, 0.2] and "text_embedding" not in doc
    assert (doc["_id"], doc["chunk_index"], doc["chunk_total"]) == ("uploads/photos/beach.jpg#0", 0, 1)
    assert (doc["bucket"], doc["filename"], doc["extension"], doc["mime_type"]) == ("uploads", "beach.jpg", "jpg", "image/jpeg")
    for word in ("beach.jpg", "Canon", "EOS R5", "2024:03:15"):
        assert word in doc["chunk_text"]
    assert len(model.images) == 1


def test_large_image_is_bounded_and_upright_before_embedding():
    model = FakeModel()
    big = Image.new("RGB", (4000, 1000))
    exif = Image.Exif()
    exif[274] = 6  # rotate 90 degrees: the upright image is 1000 wide, 4000 tall
    buf = io.BytesIO()
    big.save(buf, "JPEG", exif=exif)
    asyncio.run(worker(FakeIndex(), minio=FakeMinio(buf.getvalue()), model=model).process_event(image_event()))
    sent = Image.open(io.BytesIO(model.images[0]))
    assert max(sent.size) == 2048 and sent.height > sent.width


def test_undecodable_image_raises_so_the_message_reaches_the_dlq():
    index = FakeIndex()
    with pytest.raises(Exception):
        asyncio.run(worker(index, minio=FakeMinio(b"not an image")).process_event(image_event()))
    assert index.docs == []


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


def test_model_client_posts_image_bytes_and_returns_the_vector():
    def handler(request):
        assert request.url.path == "/embed/image" and request.content == b"bytes"
        return httpx.Response(200, json={"embedding": [1.0, 2.0]})

    client = ModelClient("http://x", transport=httpx.MockTransport(handler))
    assert asyncio.run(client.embed_image(b"bytes")) == [1.0, 2.0]


def test_model_client_raises_on_server_error():
    client = ModelClient("http://x", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.embed_texts(["a"]))


class FakeMessage:
    def value(self):
        return b"not json"  # fails processing, so the worker must use the dead-letter topic

    def error(self):
        return None


class FakeConsumer:
    def __init__(self, worker):
        self.worker, self.committed = worker, []

    def poll(self, timeout):
        self.worker.stop = True  # one message, then the loop ends
        return FakeMessage()

    def commit(self, msg):
        self.committed.append(msg)

    def close(self):
        pass


class FakeDlq:
    def __init__(self, delivery_error=None):
        self.delivery_error, self.produced = delivery_error, []

    def produce(self, topic, value, on_delivery):
        self.produced.append(topic)
        self._report = on_delivery

    def flush(self, timeout):
        self._report(self.delivery_error, None)
        return 0  # nothing left queued, whether or not delivery succeeded


def test_failed_message_is_committed_once_the_dead_letter_write_is_confirmed():
    w = worker(FakeIndex())
    consumer, dlq = FakeConsumer(w), FakeDlq()
    asyncio.run(w.run(consumer, dlq, "dlq"))
    assert dlq.produced == ["dlq"] and len(consumer.committed) == 1


def test_offset_is_not_committed_when_the_dead_letter_write_fails():
    w = worker(FakeIndex())
    consumer, dlq = FakeConsumer(w), FakeDlq(delivery_error="broker rejected the message")
    with pytest.raises(RuntimeError, match="dead-letter"):
        asyncio.run(w.run(consumer, dlq, "dlq"))
    assert consumer.committed == []
