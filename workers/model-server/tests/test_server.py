import io
import json
import pathlib

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import server

ROOT = pathlib.Path(__file__).resolve().parents[1]

SPEC = json.loads((ROOT / "model_spec.json").read_text())
DIM = SPEC["text"]["dimension"]
IMG_DIM = SPEC["image"]["dimension"]


class FakeEmbedder:
    name = "fake"
    device = "cpu"

    def embed(self, texts):
        return [[float(len(t))] * DIM for t in texts]

    def embed_query(self, text):  # models with a query prefix embed queries differently from passages
        return [-float(len(text))] * DIM


class FakeReranker:
    name = "fake-rerank"

    def score(self, query, passages):
        return [float(len(p)) - 5 for p in passages]


class FakeImageEmbedder:
    name = "fake-image"
    device = "cpu"

    def embed_images(self, images):
        return [[float(i.width)] * IMG_DIM for i in images]

    def embed_texts(self, texts):
        return [[-1.0] * IMG_DIM for _ in texts]


def png(width=3):
    buf = io.BytesIO()
    Image.new("RGB", (width, 2)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client():
    server.text_embedder = FakeEmbedder()
    server.reranker = FakeReranker()
    server.image_embedder = FakeImageEmbedder()
    server.answerer = None
    return TestClient(server.app)  # not entered as a context manager: no real model load


def test_embed_text_returns_one_vector_per_input_with_spec_dimension(client):
    r = client.post("/embed/text", json={"texts": ["a", "bb", "ccc"]})
    assert r.status_code == 200
    vectors = r.json()["embeddings"]
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0]
    assert all(len(v) == DIM for v in vectors)


def test_embed_query_returns_text_and_image_vectors(client):
    r = client.post("/embed/query", json={"query": "hello"})
    assert r.status_code == 200
    assert len(r.json()["text_vector"]) == DIM
    assert r.json()["image_vector"] == [-1.0] * IMG_DIM


def test_embed_image_returns_one_vector_of_spec_dimension(client):
    r = client.post("/embed/image", content=png(7))
    assert r.status_code == 200
    assert r.json()["embedding"] == [7.0] * IMG_DIM


@pytest.mark.parametrize("content", [b"", b"not an image"])
def test_embed_image_rejects_empty_or_undecodable_upload(client, content):
    assert client.post("/embed/image", content=content).status_code == 422


@pytest.mark.parametrize("body", [{"texts": []}, {"texts": ["ok", "  "]}, {}])
def test_embed_text_rejects_empty_input(client, body):
    assert client.post("/embed/text", json=body).status_code == 422


@pytest.mark.parametrize("body", [{"query": ""}, {"query": "   "}, {}])
def test_embed_query_rejects_empty_input(client, body):
    assert client.post("/embed/query", json=body).status_code == 422


def test_health_reports_model_and_device(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "device": "cpu",
                        "models": {"text": "fake", "image": "fake-image", "reranker": "fake-rerank", "answer": None}}


@pytest.mark.parametrize("missing", ["text_embedder", "image_embedder", "reranker"])
def test_health_is_503_until_every_model_is_loaded(client, missing):
    setattr(server, missing, None)
    assert TestClient(server.app).get("/health").status_code == 503


def test_rerank_highlights_the_best_sentence_of_the_top_passages(client):
    r = client.post("/rerank", json={"query": "q", "passages": ["Aa. Bbbbbbb.", "Cc. Dd.", "Ee. Ffffffffffff."], "highlight_top": 1})
    assert r.json()["highlights"] == ["", "", "Ffffffffffff."]


def test_rerank_without_highlight_top_returns_empty_highlights(client):
    r = client.post("/rerank", json={"query": "q", "passages": ["Aa. Bb."]})
    assert r.json()["highlights"] == [""]


def test_rerank_returns_one_raw_score_per_passage_in_input_order(client):
    r = client.post("/rerank", json={"query": "q", "passages": ["abcdefgh", "a", ""]})
    assert r.status_code == 200
    assert r.json()["scores"] == [3.0, -4.0, -5.0]


@pytest.mark.parametrize("body", [{"query": "", "passages": ["a"]}, {"query": "  ", "passages": ["a"]}, {"query": "q", "passages": []}, {"query": "q"}, {}])
def test_rerank_rejects_blank_query_and_empty_passages(client, body):
    assert client.post("/rerank", json=body).status_code == 422


def test_reranker_name_comes_from_the_spec():
    assert json.loads((ROOT / "model_spec.json").read_text())["reranker"]["name"]


class FakeAnswerer:
    name = "fake-answer"

    def answer(self, question, sources):
        return f"{question}: " + ", ".join(s["filename"] for s in sources)


SOURCES = [{"filename": "a.pdf", "passage": "pa"}, {"filename": "b.txt", "passage": "pb"}]


def test_answer_is_503_when_the_model_is_not_loaded_and_health_still_passes(client):
    assert client.post("/answer", json={"question": "q", "sources": SOURCES}).status_code == 503
    assert client.get("/health").status_code == 200


def test_answer_uses_the_loaded_model_and_health_names_it(client):
    server.answerer = FakeAnswerer()
    r = client.post("/answer", json={"question": "why", "sources": SOURCES})
    assert r.status_code == 200 and r.json() == {"answer": "why: a.pdf, b.txt"}
    assert client.get("/health").json()["models"]["answer"] == "fake-answer"


@pytest.mark.parametrize("body", [{"question": " ", "sources": SOURCES}, {"question": "q", "sources": []}, {"question": "q"}])
def test_answer_rejects_blank_question_and_no_sources(client, body):
    server.answerer = FakeAnswerer()
    assert client.post("/answer", json=body).status_code == 422


class FakeCuda:
    def __init__(self, available):
        self.cuda = type("C", (), {"is_available": staticmethod(lambda: available)})


@pytest.mark.parametrize("enabled,cuda,loads", [("1", True, True), ("1", False, False), ("", True, False)])
def test_answer_model_loads_only_when_enabled_and_cuda_is_visible(monkeypatch, enabled, cuda, loads):
    import sys
    monkeypatch.setenv("ANSWER_ENABLED", enabled)
    monkeypatch.setitem(sys.modules, "torch", FakeCuda(cuda))
    monkeypatch.setitem(sys.modules, "answerer", type("M", (), {"Answerer": lambda **kw: FakeAnswerer()}))
    assert (server.load_answerer() is not None) == loads


def test_answer_model_that_fails_to_load_does_not_stop_the_server(monkeypatch):
    import sys

    def broken(**kw):
        raise RuntimeError("out of GPU memory")

    monkeypatch.setenv("ANSWER_ENABLED", "1")
    monkeypatch.setitem(sys.modules, "torch", FakeCuda(True))
    monkeypatch.setitem(sys.modules, "answerer", type("M", (), {"Answerer": broken}))
    assert server.load_answerer() is None
