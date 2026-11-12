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
        return io.BytesIO(self.data)  # like the real response: read(amt) and close()


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
        self.docs, self.deleted, self.fail_bulk = [], [], False

    def bulk_index(self, docs):
        if self.fail_bulk:
            raise RuntimeError("bulk failed")
        ids = {d["_id"] for d in docs}  # same id overwrites, as in OpenSearch
        self.docs = [d for d in self.docs if d["_id"] not in ids] + list(docs)

    def delete_by_object_key(self, bucket, key, keep_ids=()):
        self.deleted.append((bucket, key))
        self.docs = [d for d in self.docs
                     if (d["bucket"], d["object_key"]) != (bucket, key) or d["_id"] in keep_ids]


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


def test_oversized_file_raises_naming_size_and_limit_and_is_not_downloaded():
    minio = FakeMinio()
    minio.get_object = None  # must not be called
    with pytest.raises(RuntimeError, match=r"too large.*200000000.*104857600"):
        asyncio.run(worker(FakeIndex(), minio=minio).process_event(created_event(size=200_000_000)))


def test_one_failing_record_does_not_skip_the_others_in_the_event():
    event = created_event("a.txt")
    event["Records"] += created_event("b.txt", size=200_000_000)["Records"] + created_event("c.txt")["Records"]
    index = FakeIndex()
    with pytest.raises(RuntimeError, match="b.txt"):
        asyncio.run(worker(index).process_event(event))
    assert {d["object_key"] for d in index.docs} == {"a.txt", "c.txt"}


@pytest.mark.parametrize("text", ["", "   \n", "too short"])
def test_file_without_extractable_text_is_indexed_by_filename(text):
    index = FakeIndex()
    tika = FakeTika(ExtractionResult(text=text, metadata={"dc:title": "Scan 7", "dc:creator": "Ana"}))
    asyncio.run(worker(index, tika).process_event(created_event("scans/invoice.pdf")))

    (doc,) = index.docs
    assert (doc["_id"], doc["chunk_index"], doc["chunk_total"]) == ("uploads/scans/invoice.pdf#0", 0, 1)
    assert "text_embedding" not in doc and "image_embedding" not in doc
    for word in ("invoice.pdf", "Scan 7", "Ana"):
        assert word in doc["chunk_text"]
    assert (doc["bucket"], doc["filename"], doc["extension"], doc["size_bytes"]) == ("uploads", "invoice.pdf", "pdf", 2400)


def test_reupload_with_fewer_chunks_leaves_only_the_new_chunks():
    index = FakeIndex()
    asyncio.run(worker(index).process_event(created_event()))
    assert len(index.docs) > 2
    short = ExtractionResult(text="Only two short paragraphs here.\n\n" + "Second paragraph text. " * 20)
    asyncio.run(worker(index, FakeTika(short)).process_event(created_event()))
    assert sorted(d["chunk_index"] for d in index.docs) == list(range(len(index.docs))) and len(index.docs) <= 2


def test_replacing_text_with_image_or_no_text_leaves_one_document():
    index = FakeIndex()
    asyncio.run(worker(index).process_event(created_event("f")))
    assert len(index.docs) > 1
    asyncio.run(worker(index, minio=FakeMinio(jpeg())).process_event(image_event("f")))
    assert [d["_id"] for d in index.docs] == ["uploads/f#0"] and "image_embedding" in index.docs[0]
    asyncio.run(worker(index, FakeTika(ExtractionResult(text=""))).process_event(created_event("f")))
    assert len(index.docs) == 1 and "image_embedding" not in index.docs[0]


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
    def __init__(self, value=b"not json", headers=None):  # bad JSON fails processing: dead-letter path
        self._value, self._headers = value, headers

    def value(self):
        return self._value

    def headers(self):
        return self._headers

    def error(self):
        return None


class FakeConsumer:
    def __init__(self, worker, messages=None):
        self.worker, self.messages, self.committed, self.closed = worker, list(messages or [FakeMessage()]), [], False

    def poll(self, timeout):
        msg = self.messages.pop(0)
        self.worker.stop = not self.messages  # the loop ends after the last message
        return msg

    def commit(self, msg, asynchronous=True):
        assert asynchronous is False  # an async commit can be lost on shutdown or rebalance
        self.committed.append(msg)

    def close(self):
        self.closed = True


class FakeDlq:
    def __init__(self, delivery_error=None):
        self.delivery_error, self.produced = delivery_error, []

    def produce(self, topic, value, on_delivery, headers=None):
        self.produced.append((topic, value, headers))
        self._report = on_delivery

    def flush(self, timeout):
        self._report(self.delivery_error, None)
        return 0  # nothing left queued, whether or not delivery succeeded


def test_failed_message_is_committed_once_the_dead_letter_write_is_confirmed():
    w = worker(FakeIndex())
    consumer, dlq = FakeConsumer(w), FakeDlq()
    asyncio.run(w.run(consumer, dlq, "dlq"))
    assert [p[0] for p in dlq.produced] == ["dlq"] and len(consumer.committed) == 1


def test_offset_is_not_committed_when_the_dead_letter_write_fails():
    w = worker(FakeIndex())
    consumer, dlq = FakeConsumer(w), FakeDlq(delivery_error="broker rejected the message")
    with pytest.raises(RuntimeError, match="could not deliver"):
        asyncio.run(w.run(consumer, dlq, "dlq"))
    assert consumer.committed == []
    assert consumer.closed  # leave the group at once so the partition is reassigned


def test_messages_are_committed_in_the_order_they_were_polled():
    w = worker(FakeIndex())
    messages = [FakeMessage() for _ in range(3)]
    consumer = FakeConsumer(w, messages)
    asyncio.run(w.run(consumer, FakeDlq(), "dlq"))
    assert consumer.committed == messages


def test_stop_during_a_message_lets_it_finish_and_commit_then_closes_without_polling_again():
    w = worker(FakeIndex())
    messages = [FakeMessage(b"{}"), FakeMessage(b"{}")]  # valid JSON: reaches process_event
    consumer = FakeConsumer(w, messages)

    async def stop_while_processing(event):
        w.stop = True  # SIGTERM arrives mid-message
    w.process_event = stop_while_processing
    asyncio.run(w.run(consumer, FakeDlq(), "dlq"))
    assert consumer.committed == messages[:1] and len(consumer.messages) == 1 and consumer.closed


def test_a_failure_during_a_stop_is_not_committed_when_it_cannot_be_dead_lettered():
    w = worker(FakeIndex())
    consumer = FakeConsumer(w, [FakeMessage(), FakeMessage()])
    with pytest.raises(RuntimeError):
        asyncio.run(w.run(consumer, FakeDlq(delivery_error="down"), "dlq"))
    assert consumer.committed == []


def test_retry_count_header_is_carried_onto_the_dead_letter_record():
    w = worker(FakeIndex())
    dlq = FakeDlq()
    asyncio.run(w.run(FakeConsumer(w, [FakeMessage(headers=[("retry-count", b"2")])]), dlq, "dlq"))
    assert dlq.produced[0][2] == [("retry-count", b"2")]


def test_a_first_failure_has_no_retry_header_to_carry():
    w = worker(FakeIndex())
    dlq = FakeDlq()
    asyncio.run(w.run(FakeConsumer(w), dlq, "dlq"))
    assert dlq.produced[0][2] == []


def test_download_never_reads_past_the_size_limit_even_when_the_event_size_is_wrong(monkeypatch):
    monkeypatch.setattr("main.MAX_FILE_SIZE_BYTES", 100)
    reads = []

    class Spy(io.BytesIO):
        def read(self, amt=-1):
            reads.append(amt)
            return super().read(amt)

    class SpyMinio(FakeMinio):
        def get_object(self, bucket, key):
            return Spy(self.data)

    index = FakeIndex()
    with pytest.raises(RuntimeError, match="too large"):
        asyncio.run(worker(index, minio=SpyMinio(b"x" * 500)).process_event(created_event(size=10)))
    assert reads == [101] and index.docs == []


def test_download_runs_off_the_event_loop_thread():
    import threading
    seen = []

    class ThreadMinio(FakeMinio):
        def get_object(self, bucket, key):
            seen.append(threading.current_thread())
            return super().get_object(bucket, key)

    asyncio.run(worker(FakeIndex(), minio=ThreadMinio()).process_event(created_event()))
    assert seen and seen[0] is not threading.main_thread()


def test_failed_reindex_keeps_the_previous_version_searchable():
    index = FakeIndex()
    asyncio.run(worker(index).process_event(created_event()))
    before = list(index.docs)
    index.fail_bulk = True
    with pytest.raises(RuntimeError, match="bulk failed"):
        asyncio.run(worker(index).process_event(created_event()))
    assert index.docs == before
