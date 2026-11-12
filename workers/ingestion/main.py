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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ingestion")

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024


class IngestionWorker:
    def __init__(self, minio, tika, chunker, model, index):
        self.minio, self.tika, self.chunker, self.model, self.index = minio, tika, chunker, model, index
        self.stop = False

    async def process_event(self, event: dict) -> None:
        for record in event.get("Records", []):
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
            raise RuntimeError(f"file too large: {size} bytes")

        response = self.minio.get_object(bucket, key)
        try:
            data = response.read()
        finally:
            response.close()

        if mime == "application/octet-stream":
            mime = await self.tika.detect_type(data) or mime
        if mime.startswith("image/"):
            log.warning("skipped %s/%s: images are not indexed yet", bucket, key)
            return

        extracted = await self.tika.extract(data, mime)
        if not extracted.success:
            raise RuntimeError(extracted.error)
        chunks = self.chunker.chunk(extracted.text)
        if not chunks:
            log.warning("skipped %s/%s: no text extracted", bucket, key)
            return
        vectors = await self.model.embed_texts([c.text for c in chunks])

        filename = key.rsplit("/", 1)[-1]
        meta = extracted.metadata
        common = {
            "bucket": bucket,
            "object_key": key,
            "filename": filename,
            "extension": filename.rsplit(".", 1)[-1].lower() if "." in filename else "",
            "mime_type": mime,
            "size_bytes": size,
            "uploaded_at": record["eventTime"],
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_total": len(chunks),
            "title": meta.get("dc:title") or filename,
            "author": meta.get("dc:creator") or "",
        }
        self.index.bulk_index([
            {**common, "_id": f"{bucket}/{key}#{c.chunk_index}", "chunk_index": c.chunk_index,
             "chunk_text": c.text, "text_embedding": v}
            for c, v in zip(chunks, vectors)
        ])
        log.info("indexed %s/%s: %d chunks", bucket, key, len(chunks))

    async def run(self, consumer, dlq, dlq_topic: str) -> None:
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
                errors = []  # flush() only says the queue is empty, not that the write succeeded
                dlq.produce(dlq_topic, value=json.dumps(record).encode(),
                            on_delivery=lambda err, _msg: err and errors.append(err))
                if dlq.flush(10) or errors:
                    raise RuntimeError("could not reach the dead-letter topic; not committing") from exc
            consumer.commit(msg)
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
                         "enable.auto.commit": False})
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
