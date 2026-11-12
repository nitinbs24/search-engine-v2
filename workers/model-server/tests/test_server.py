import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import server

ROOT = pathlib.Path(__file__).resolve().parents[1]

DIM = json.loads((ROOT / "model_spec.json").read_text())["text"]["dimension"]


class FakeEmbedder:
    name = "fake"
    device = "cpu"

    def embed(self, texts):
        return [[float(len(t))] * DIM for t in texts]


@pytest.fixture
def client():
    server.text_embedder = FakeEmbedder()
    return TestClient(server.app)  # not entered as a context manager: no real model load


def test_embed_text_returns_one_vector_per_input_with_spec_dimension(client):
    r = client.post("/embed/text", json={"texts": ["a", "bb", "ccc"]})
    assert r.status_code == 200
    vectors = r.json()["embeddings"]
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0]
    assert all(len(v) == DIM for v in vectors)


def test_embed_query_returns_text_vector(client):
    r = client.post("/embed/query", json={"query": "hello"})
    assert r.status_code == 200
    assert len(r.json()["text_vector"]) == DIM


@pytest.mark.parametrize("body", [{"texts": []}, {"texts": ["ok", "  "]}, {}])
def test_embed_text_rejects_empty_input(client, body):
    assert client.post("/embed/text", json=body).status_code == 422


@pytest.mark.parametrize("body", [{"query": ""}, {"query": "   "}, {}])
def test_embed_query_rejects_empty_input(client, body):
    assert client.post("/embed/query", json=body).status_code == 422


def test_health_reports_model_and_device(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "device": "cpu", "models": {"text": "fake"}}


def test_health_is_503_until_model_is_loaded():
    server.text_embedder = None
    assert TestClient(server.app).get("/health").status_code == 503
