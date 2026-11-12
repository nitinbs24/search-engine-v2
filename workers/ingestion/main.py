"""Ingestion worker: MinIO upload event -> Tika -> chunks -> model server -> OpenSearch.

Messages are handled one at a time, in order, and the offset is committed only after
the message is indexed or parked on the dead-letter topic.
"""
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import image_handler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ingestion")

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024
RETRY_HEADER = "retry-count"  # how many times the dead-letter retry worker has re-sent the event


def deliver(producer, topic, value, headers=None):
    """Produce and wait until the broker confirms. flush() only says the queue is empty,
    not that the write succeeded, so the delivery callback is checked as well."""
    errors = []
    producer.produce(topic, value=value, headers=headers, on_delivery=lambda err, _msg: err and errors.append(err))
    if producer.flush(10) or errors:
        raise RuntimeError(f"could not deliver to {topic}; not committing")


class IngestionWorker:
    def __init__(self, minio, tika, chunker, model, index):
        self.minio, self.tika, self.chunker, self.model, self.index = minio, tika, chunker, model, index
        self.stop = False

    async def process_event(self, event: dict) -> None:
        # Every record is attempted; failures are raised together so the whole message is dead-lettered
        # (redelivery is safe: ids are stable and re-ingesting replaces an object's chunks).
        failures = []
        for record in event.get("Records", []):
            try:
                await self._process_record(record)
            except Exception as exc:
                log.exception("record failed")
                failures.append(f"{record.get('s3', {}).get('object', {}).get('key')}: {exc}")
        if failures:
            raise RuntimeError("; ".join(failures))

    async def _process_record(self, record: dict) -> None:
        name = record.get("eventName", "")
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        if name.startswith("s3:ObjectRemoved"):
            self.index.delete_by_object_key(bucket, key)
            log.info("removed %s/%s", bucket, key)
        elif name.startswith("s3:ObjectCreated"):
            await self._ingest(record, bucket, key)

    async def _ingest(self, record: dict, bucket: str, key: str) -> None:
        obj = record["s3"]["object"]
        size = int(obj.get("size", 0))
        mime = obj.get("contentType") or "application/octet-stream"
        if size > MAX_FILE_SIZE_BYTES:
            raise RuntimeError(f"file too large: {size} bytes exceeds the {MAX_FILE_SIZE_BYTES} byte limit")

        def download() -> bytes:
            response = self.minio.get_object(bucket, key)
            try:
                # The event's size may be wrong or missing: never read more than the limit plus one byte.
                return response.read(MAX_FILE_SIZE_BYTES + 1)
            finally:
                response.close()

        data = await asyncio.to_thread(download)
        if len(data) > MAX_FILE_SIZE_BYTES:
            raise RuntimeError(f"file too large: over {MAX_FILE_SIZE_BYTES} bytes")

        if mime == "application/octet-stream":
            mime = await self.tika.detect_type(data) or mime
        filename = key.rsplit("/", 1)[-1]
        common = {
            "bucket": bucket,
            "object_key": key,
            "filename": filename,
            "extension": filename.rsplit(".", 1)[-1].lower() if "." in filename else "",
            "mime_type": mime,
            "size_bytes": size,
            "uploaded_at": record["eventTime"],
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "title": filename,
            "author": "",
        }
        if mime.startswith("image/"):
            # Raises on undecodable bytes, so the message goes to the dead-letter topic.
            jpeg, facts = await asyncio.to_thread(image_handler.prepare, data)
            self._replace(bucket, key, [{
                **common, "_id": f"{bucket}/{key}#0", "chunk_index": 0, "chunk_total": 1,
                "chunk_text": image_handler.describe(filename, facts),
                "image_embedding": await self.model.embed_image(jpeg),
            }])
            log.info("indexed image %s/%s", bucket, key)
            return

        extracted = await self.tika.extract(data, mime)
        if not extracted.success:
            raise RuntimeError(extracted.error)
        chunks = self.chunker.chunk(extracted.text)
        meta = extracted.metadata
        common |= {"title": meta.get("dc:title") or filename, "author": meta.get("dc:creator") or ""}
        if not chunks:
            # No text (empty, too short, scanned PDF): keep the file findable by name and metadata, with no vector.
            names = dict.fromkeys([filename, common["title"], common["author"]])
            self._replace(bucket, key, [{
                **common, "_id": f"{bucket}/{key}#0", "chunk_index": 0, "chunk_total": 1,
                "chunk_text": " ".join(n for n in names if n),
            }])
            log.warning("indexed %s/%s by filename only: no text extracted", bucket, key)
            return
        vectors = await self.model.embed_texts([c.text for c in chunks])
        common["chunk_total"] = len(chunks)
        self._replace(bucket, key, [
            {**common, "_id": f"{bucket}/{key}#{c.chunk_index}", "chunk_index": c.chunk_index,
             "chunk_text": c.text, "text_embedding": v}
            for c, v in zip(chunks, vectors)
        ])
        log.info("indexed %s/%s: %d chunks", bucket, key, len(chunks))

    def _replace(self, bucket: str, key: str, docs: list[dict]) -> None:
        # Index first (stable ids overwrite the old chunks), then remove what the old version left
        # behind. If indexing fails, the previous version stays searchable.
        self.index.bulk_index(docs)
        self.index.delete_by_object_key(bucket, key, keep_ids=[d["_id"] for d in docs])

    async def run(self, consumer, dlq, dlq_topic: str) -> None:
        try:
            while not self.stop:
                msg = await asyncio.to_thread(consumer.poll, 1.0)
                if msg is None:
                    continue
                if msg.error():
                    log.error("kafka: %s", msg.error())
                    continue
                try:
                    await self.process_event(json.loads(msg.value()))
                except Exception as exc:
                    log.exception("failed, sending to the dead-letter topic")
                    record = {"original_payload": msg.value().decode("utf-8", "replace"), "error": str(exc),
                              "failed_at": datetime.now(timezone.utc).isoformat()}
                    # Keep the retry count so it survives the round trip through the retry worker.
                    retry = [h for h in msg.headers() or [] if h[0] == RETRY_HEADER]
                    deliver(dlq, dlq_topic, json.dumps(record).encode(), retry)
                # Synchronous: the default async commit can be lost if we stop right after.
                consumer.commit(msg, asynchronous=False)
        finally:
            consumer.close()


def main() -> None:
    from confluent_kafka import Consumer, Producer
    from minio import Minio

    from chunker import TextChunker
    from model_client import ModelClient
    from opensearch_client import Index
    from tika_extractor import TikaExtractor

    env = os.environ
    kafka = {"bootstrap.servers": env["KAFKA_BOOTSTRAP_SERVERS"]}
    consumer = Consumer({**kafka, "group.id": "ingestion-worker", "auto.offset.reset": "earliest",
                         "enable.auto.commit": False,
                         # One file can take minutes (large PDF embedded on CPU) between polls.
                         "max.poll.interval.ms": 30 * 60 * 1000})
    consumer.subscribe([env["KAFKA_TOPIC"]])
    worker = IngestionWorker(
        Minio(env["MINIO_ENDPOINT"], access_key=env["MINIO_ROOT_USER"], secret_key=env["MINIO_ROOT_PASSWORD"],
              secure=False),
        TikaExtractor(tika_url=env["TIKA_URL"]),
        TextChunker(),
        ModelClient(env["MODEL_SERVER_URL"]),
        Index(env["OPENSEARCH_URL"], env["OPENSEARCH_INDEX"]),
    )
    signal.signal(signal.SIGTERM, lambda *_: setattr(worker, "stop", True))
    log.info("ingestion worker started")
    asyncio.run(worker.run(consumer, Producer({**kafka, "acks": "all"}), env["KAFKA_DLQ_TOPIC"]))


if __name__ == "__main__":
    main()
