import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from opensearchpy.exceptions import RequestError, TransportError

from opensearch_client import Index

DOC = {"_id": "b/k#0", "bucket": "b", "object_key": "k"}
BODIES = []  # request bodies seen by the fake server


@pytest.fixture
def server():
    """A fake OpenSearch on loopback: replies with the queued (status, body) and counts requests."""
    replies, hits = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            BODIES.append(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            hits.append(self.path)
            status, body = replies.pop(0) if len(replies) > 1 else replies[0]
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_PUT = do_POST

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Index(f"http://127.0.0.1:{httpd.server_port}", "idx"), replies, hits
    httpd.shutdown()


def bulk_reply(*statuses):
    items = [{"index": {"_id": f"b/k#{i}", "status": s, **({"error": {"type": "x"}} if s >= 300 else {})}}
             for i, s in enumerate(statuses)]
    return 200, {"errors": any(s >= 300 for s in statuses), "items": items}


def test_bulk_with_one_failed_item_raises(server):
    index, replies, _ = server
    replies.append(bulk_reply(201, 400, 201))
    with pytest.raises(Exception, match="1 document"):
        index.bulk_index([DOC, {**DOC, "_id": "b/k#1"}, {**DOC, "_id": "b/k#2"}])


def test_bulk_with_all_items_ok_passes(server):
    index, replies, _ = server
    replies.append(bulk_reply(201))
    index.bulk_index([DOC])


def test_client_error_is_not_retried(server):
    index, replies, hits = server
    replies.append((400, {"error": {"type": "mapper_parsing_exception"}}))
    with pytest.raises(RequestError):
        index.bulk_index([DOC])
    assert len(hits) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_overload_and_server_errors_are_retried(server, status):
    index, replies, hits = server
    replies.extend([(status, {}), bulk_reply(201)])
    index.bulk_index([DOC])
    assert len(hits) == 2


def test_item_level_overload_is_retried_not_failed(server):
    index, replies, hits = server
    replies.extend([bulk_reply(429), bulk_reply(201)])  # HTTP 200 both times; the first item is throttled
    index.bulk_index([DOC])
    assert len(hits) == 2


def test_delete_can_spare_the_chunks_just_indexed(server):
    index, replies, _ = server
    BODIES.clear()
    replies.append((200, {"deleted": 4}))
    index.delete_by_object_key("b", "k", keep_ids=["b/k#0", "b/k#1"])
    query = json.loads(BODIES[-1])["query"]["bool"]
    assert query["must_not"] == [{"ids": {"values": ["b/k#0", "b/k#1"]}}]
    assert {"term": {"object_key": "k"}} in query["filter"]
