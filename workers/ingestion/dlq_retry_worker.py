"""Dead-letter retry worker: re-sends failed upload events to the main topic, at most MAX_RETRIES times.

Reads only the dead-letter topic and writes only the main and permanent-failure topics, so it
never consumes its own output. The ingestion worker copies the retry header onto the new
dead-letter record when a retry fails, which is how the count survives the round trip.
"""
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone

from main import RETRY_HEADER, deliver

log = logging.getLogger("dlq-retry")

MAX_RETRIES = 3
# Gives a briefly-down dependency (Tika, model server) time to recover. Far below Kafka's
# max.poll.interval.ms (5 min default), so waiting in the loop cannot get us kicked from the group.
RETRY_DELAY_S = 30


class RetryWorker:
    def __init__(self, main_topic: str, dlq_topic: str, failed_topic: str):
        if len({main_topic, dlq_topic, failed_topic}) != 3:
            raise ValueError("main, dead-letter and permanent-failure topics must be different")
        self.main_topic, self.failed_topic, self.dlq_topic = main_topic, failed_topic, dlq_topic
        self.stop = False

    def handle(self, producer, msg) -> None:
        record = json.loads(msg.value())
        count = int(dict(msg.headers() or []).get(RETRY_HEADER, b"0"))
        if count >= MAX_RETRIES:
            record["retries"] = count
            deliver(producer, self.failed_topic, json.dumps(record).encode())
            log.error("giving up after %d retries: %s", count, record.get("error"))
            return
        # Delay is counted from the failure time, so a backlog of records does not add up.
        due = datetime.fromisoformat(record["failed_at"]).timestamp() + RETRY_DELAY_S
        time.sleep(min(max(due - time.time(), 0), RETRY_DELAY_S))
        deliver(producer, self.main_topic, record["original_payload"].encode(),
                [(RETRY_HEADER, str(count + 1).encode())])
        log.info("retry %d of %d", count + 1, MAX_RETRIES)

    def run(self, consumer, producer) -> None:
        try:
            while not self.stop:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    log.error("kafka: %s", msg.error())
                    continue
                self.handle(producer, msg)
                consumer.commit(msg, asynchronous=False)  # only after delivery was confirmed
        finally:
            consumer.close()


def main() -> None:
    from confluent_kafka import Consumer, Producer

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = os.environ
    kafka = {"bootstrap.servers": env["KAFKA_BOOTSTRAP_SERVERS"]}
    worker = RetryWorker(env["KAFKA_TOPIC"], env["KAFKA_DLQ_TOPIC"], env["KAFKA_DLQ_FAILED_TOPIC"])
    consumer = Consumer({**kafka, "group.id": "dlq-retry-worker", "auto.offset.reset": "earliest",
                         "enable.auto.commit": False})
    consumer.subscribe([worker.dlq_topic])
    signal.signal(signal.SIGTERM, lambda *_: setattr(worker, "stop", True))
    log.info("dead-letter retry worker started")
    worker.run(consumer, Producer({**kafka, "acks": "all"}))


if __name__ == "__main__":
    main()
