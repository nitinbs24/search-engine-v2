import json

import dlq_retry_worker
from dlq_retry_worker import MAX_RETRIES, RetryWorker

MAIN, DLQ, FAILED = "events", "events-dlq", "events-dlq-failed"
PAYLOAD = '{"Records": []}'


class Msg:
    def __init__(self, value, headers=None):
        self._value, self._headers = value, headers

    def value(self):
        return self._value

    def headers(self):
        return self._headers

    def error(self):
        return None


class Broker:
    """One fake for the whole loop: the main topic's consumer (ingestion) always fails,
    for fail_times retries. Everything produced is recorded in order."""

    def __init__(self, fail_times):
        self.fail_times, self.produced, self.committed, self.queue, self.stop = fail_times, [], [], [], None

    def produce(self, topic, value, on_delivery, headers=None):
        self.produced.append((topic, value, headers))
        self._last = on_delivery
        if topic == MAIN:  # the ingestion worker picks it up; it fails fail_times times, then succeeds
            if self.fail_times:
                self.fail_times -= 1
                record = {"original_payload": value.decode(), "error": "boom", "failed_at": "2000-01-01T00:00:00+00:00"}
                self.queue.append(Msg(json.dumps(record).encode(), headers))

    def flush(self, timeout):
        self._last(None, None)
        return 0

    def poll(self, timeout):
        if not self.queue:
            self.stop()
            return None
        return self.queue.pop(0)

    def commit(self, msg, asynchronous=True):
        assert asynchronous is False
        self.committed.append(msg)

    def close(self):
        pass


def run(fail_times):
    """Start from one dead-lettered message (count 0, as ingestion writes it)."""
    w = RetryWorker(MAIN, DLQ, FAILED)
    broker = Broker(fail_times)
    broker.stop = lambda: setattr(w, "stop", True)
    broker.queue.append(Msg(json.dumps({"original_payload": PAYLOAD, "error": "boom",
                                        "failed_at": "2000-01-01T00:00:00+00:00"}).encode()))
    w.run(broker, broker)
    return broker


def topics(broker):
    return [t for t, _, _ in broker.produced]


def test_three_consecutive_failures_end_in_three_retries_and_one_permanent_record():
    broker = run(fail_times=MAX_RETRIES)
    assert topics(broker) == [MAIN] * 3 + [FAILED]
    assert broker.produced[0][1] == PAYLOAD.encode()  # the original event, unchanged
    assert [dict(h)["retry-count"] for _, _, h in broker.produced[:3]] == [b"1", b"2", b"3"]
    assert json.loads(broker.produced[3][1])["original_payload"] == PAYLOAD
    assert len(broker.committed) == 4  # every DLQ record committed once


def test_message_that_succeeds_on_the_first_retry_produces_nothing_more():
    broker = run(fail_times=0)
    assert topics(broker) == [MAIN] and len(broker.committed) == 1


def test_never_writes_to_the_topic_it_reads():
    assert DLQ not in topics(run(fail_times=MAX_RETRIES))


def test_offset_is_not_committed_when_the_republish_is_not_confirmed():
    w = RetryWorker(MAIN, DLQ, FAILED)
    broker = Broker(fail_times=0)
    broker.stop = lambda: setattr(w, "stop", True)
    broker.flush = lambda timeout: 1  # still queued: not delivered
    broker.queue.append(Msg(json.dumps({"original_payload": PAYLOAD, "failed_at": "2000-01-01T00:00:00+00:00"}).encode()))
    try:
        w.run(broker, broker)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the worker to stop")
    assert broker.committed == []


def test_delay_is_bounded_and_measured_from_the_failure_time(monkeypatch):
    from datetime import datetime, timezone
    sleeps = []
    monkeypatch.setattr(dlq_retry_worker.time, "sleep", sleeps.append)
    w = RetryWorker(MAIN, DLQ, FAILED)
    broker = Broker(fail_times=0)
    broker.stop = lambda: setattr(w, "stop", True)
    now = datetime.now(timezone.utc).isoformat()
    broker.queue.append(Msg(json.dumps({"original_payload": PAYLOAD, "failed_at": now}).encode()))
    w.run(broker, broker)
    assert len(sleeps) == 1 and 0 < sleeps[0] <= dlq_retry_worker.RETRY_DELAY_S
