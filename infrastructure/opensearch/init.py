"""Creates the search index and the hybrid search pipeline. Safe to re-run.

The vector dimension comes from workers/model-server/model_spec.json, nowhere else.
"""
import json
import os
import sys
import urllib.error
import urllib.request

PIPELINE = "hybrid-rrf"

KEYWORD = {"type": "keyword"}
DATE = {"type": "date", "format": "strict_date_optional_time||epoch_millis"}


def vector_field(dimension: int) -> dict:
    return {
        "type": "knn_vector",
        "dimension": dimension,
        # lucene supports filtered kNN; nmslib is deprecated.
        "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"},
    }


def index_body(spec: dict) -> dict:
    return {
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "knn": True,
                "search.default_pipeline": PIPELINE,
            },
            "analysis": {
                # Splits report_q1-final.pdf into report, q1, final, pdf.
                "tokenizer": {"alnum": {"type": "pattern", "pattern": "[^A-Za-z0-9]+"}},
                "analyzer": {"filename": {"type": "custom", "tokenizer": "alnum", "filter": ["lowercase"]}},
            },
        },
        "mappings": {
            "properties": {
                "object_key": KEYWORD,
                "bucket": KEYWORD,
                "filename": {"type": "text", "analyzer": "filename"},
                "extension": KEYWORD,
                "mime_type": KEYWORD,
                "size_bytes": {"type": "long"},
                "uploaded_at": DATE,
                "indexed_at": DATE,
                "chunk_index": {"type": "integer"},
                "chunk_total": {"type": "integer"},
                "chunk_text": {"type": "text"},
                "title": {"type": "text"},
                "author": {"type": "text"},
                "text_embedding": vector_field(spec["text"]["dimension"]),
                "image_embedding": vector_field(spec["image"]["dimension"]),
            }
        },
    }


# Reciprocal rank fusion needs no per-leg weights, so adding a query leg cannot break it.
PIPELINE_BODY = {
    "description": "Fuses the BM25 and kNN legs of a hybrid query",
    "phase_results_processors": [{"score-ranker-processor": {"combination": {"technique": "rrf"}}}],
}


def ensure(request, index: str, spec: dict) -> None:
    request("PUT", f"/_search/pipeline/{PIPELINE}", PIPELINE_BODY)
    status, existing = request("GET", f"/{index}")
    if status == 404:
        request("PUT", f"/{index}", index_body(spec))
        return
    for kind in ("text", "image"):
        have = existing[index]["mappings"]["properties"].get(f"{kind}_embedding", {}).get("dimension")
        want = spec[kind]["dimension"]
        if have != want:
            sys.exit(f"index {index} has {kind}_embedding dimension {have}, model_spec.json says {want}: delete the index and re-ingest")


def http(base: str):
    def request(method, path, body=None):
        req = urllib.request.Request(
            base + path, method=method, data=json.dumps(body).encode() if body else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, {}
            sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()}")

    return request


if __name__ == "__main__":
    spec = json.load(open(os.environ["MODEL_SPEC"]))
    ensure(http(os.environ["OPENSEARCH_URL"]), os.environ["OPENSEARCH_INDEX"], spec)
    print("index and pipeline ready")
