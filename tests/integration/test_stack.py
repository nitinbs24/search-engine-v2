"""End-to-end cases against a running stack (`make up`, then `make integration`).

Everything goes through the same doors a user's request does: files are put in MinIO,
searches go through the UI's /api/search route, downloads through /api/download.
"""
import io
import os
import time
import uuid

import httpx
import pytest
from minio import Minio
from PIL import Image, ImageDraw

UI = "http://localhost:3000"
BACKEND = "http://localhost:8080"
MODEL_SERVER = "http://localhost:8000"
OPENSEARCH = "http://localhost:9200"
BUCKET = os.environ["MINIO_BUCKET"]
INDEX = os.environ["OPENSEARCH_INDEX"]

TOKEN = uuid.uuid4().hex[:8]
PREFIX = f"it-{TOKEN}"
# Long enough to be split into several chunks.
K8S_TEXT = (
    "Kubernetes schedules containers across a cluster of machines. When a container crashes, "
    "the kubelet restarts it according to the pod's restart policy, and a Deployment replaces "
    "pods that fail their health probes. "
) * 12
K8S = f"{PREFIX}/notes/kubernetes-{TOKEN}.txt"
CSV = f"{PREFIX}/sales-{TOKEN}.csv"
CSV_TEXT = "region,quarter,product,revenue\n" + "".join(
    f"north,Q{q},arabica coffee beans wholesale,{1000 * q}\nsouth,Q{q},espresso coffee capsules retail,{700 * q}\n"
    for q in range(1, 5)
)
IMAGE = f"{PREFIX}/shape-{TOKEN}.png"  # the name says nothing about the picture
K8S_QUERY = "how does kubernetes restart failed containers"


def red_circle_png() -> bytes:
    image = Image.new("RGB", (320, 240), "white")
    ImageDraw.Draw(image).ellipse((80, 40, 240, 200), fill="red")
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


PNG = red_circle_png()


@pytest.fixture(scope="session")
def minio():
    return Minio("localhost:9000", access_key=os.environ["MINIO_ROOT_USER"],
                 secret_key=os.environ["MINIO_ROOT_PASSWORD"], secure=False)


def put(minio, key, data: bytes, content_type):
    minio.put_object(BUCKET, key, io.BytesIO(data), len(data), content_type=content_type)


def indexed_chunks(key) -> int:
    r = httpx.post(f"{OPENSEARCH}/{INDEX}/_count", json={"query": {"term": {"object_key": key}}})
    return r.json()["count"]


def wait_until(check, what, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(2)
    pytest.fail(f"timed out after {timeout}s waiting until {what}")


def search(query, limit=8):
    r = httpx.get(f"{UI}/api/search", params={"q": query, "limit": limit}, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()["results"]


def keys(results):
    return [r["object_key"] for r in results]


@pytest.fixture(scope="session", autouse=True)
def corpus(minio):
    put(minio, K8S, K8S_TEXT.encode(), "text/plain")
    put(minio, CSV, CSV_TEXT.encode(), "text/csv")
    put(minio, IMAGE, PNG, "image/png")
    for key in (K8S, CSV, IMAGE):
        wait_until(lambda: indexed_chunks(key) > 0, f"{key} is indexed")
    yield
    for key in (K8S, CSV, IMAGE):
        minio.remove_object(BUCKET, key)


# ── health ────────────────────────────────────────────────────────────────────

def test_model_server_has_every_model_loaded():
    body = httpx.get(f"{MODEL_SERVER}/health").json()
    assert body["status"] == "ok" and set(body["models"]) == {"text", "image", "reranker"}


def test_backend_health_reports_its_dependencies():
    r = httpx.get(f"{BACKEND}/health")
    assert r.status_code == 200 and r.json() == {"opensearch": "ok", "search_worker": "ok"}


# ── text search ───────────────────────────────────────────────────────────────

def test_text_document_is_first_for_a_question_about_it():
    results = search(K8S_QUERY)
    assert results and results[0]["object_key"] == K8S
    assert "restart" in results[0]["snippet"].lower()


def test_a_file_with_many_chunks_appears_once():
    assert indexed_chunks(K8S) > 1
    found = keys(search(K8S_QUERY))
    assert found.count(K8S) == 1 and len(found) == len(set(found))


def test_unrelated_query_does_not_return_our_documents():
    found = keys(search("medieval falconry training methods"))
    assert K8S not in found and CSV not in found


def test_extension_filter_holds_on_every_query_leg():
    results = search("kubernetes container restart csv")
    # Without the filter on the vector leg, the Kubernetes note would leak in here.
    assert K8S not in keys(results)
    assert all(r["extension"] == "csv" for r in results)


def test_extension_filter_still_finds_the_matching_file():
    assert CSV in keys(search("coffee revenue by region csv"))


def test_size_filter_that_nothing_satisfies_returns_nothing():
    assert search("kubernetes larger than 1GB") == []


def test_same_query_twice_gives_the_same_answer():
    assert keys(search(K8S_QUERY)) == keys(search(K8S_QUERY))


# ── images ────────────────────────────────────────────────────────────────────

def test_image_is_indexed_as_a_single_document():
    assert indexed_chunks(IMAGE) == 1


def test_image_is_found_by_describing_what_it_shows():
    results = search("a red circle")
    assert IMAGE in keys(results)
    hit = next(r for r in results if r["object_key"] == IMAGE)
    assert hit["content_type"] == "image/png" and hit["extension"] == "png"
    assert K8S not in keys(results)


def test_image_is_not_returned_for_an_unrelated_text_query():
    assert IMAGE not in keys(search(K8S_QUERY))


# ── download and preview ──────────────────────────────────────────────────────

def test_download_returns_the_uploaded_bytes_for_a_key_under_a_prefix():
    r = httpx.get(f"{UI}/api/download", params={"bucket": BUCKET, "key": K8S})
    assert r.status_code == 200 and r.content == K8S_TEXT.encode()


def test_image_preview_is_served_inline_with_its_type():
    r = httpx.get(f"{UI}/api/download", params={"bucket": BUCKET, "key": IMAGE})
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["content-disposition"].startswith("inline")


def test_download_refuses_keys_that_climb_out_of_the_bucket():
    r = httpx.get(f"{UI}/api/download", params={"bucket": BUCKET, "key": "../other/secret.txt"})
    assert r.status_code == 400


# ── lifecycle ─────────────────────────────────────────────────────────────────

def test_deleted_file_disappears_from_search(minio):
    key = f"{PREFIX}/gone-{TOKEN}.txt"
    query = "zeppelin navigator charting a course over the glacier"
    put(minio, key, ("The zeppelin navigator charted a careful course over the glacier before nightfall. " * 4).encode(),
        "text/plain")
    wait_until(lambda: key in keys(search(query)), "the new file is searchable")
    minio.remove_object(BUCKET, key)
    wait_until(lambda: key not in keys(search(query)), "the deleted file is gone from results")


def test_undecodable_image_is_not_indexed(minio):
    key = f"{PREFIX}/broken-{TOKEN}.png"
    put(minio, key, b"this is not a picture", "image/png")
    try:
        time.sleep(20)  # long enough for the worker to have processed it
        assert indexed_chunks(key) == 0
    finally:
        minio.remove_object(BUCKET, key)
