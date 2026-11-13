# Architecture

A self-hosted search engine for files dropped into an object store. Text documents and images
are searchable in natural language ("pdfs about bread uploaded last week"), with the best
sentence of each hit highlighted and, optionally, a short generated answer.

## Components

```
                 upload (mc, console, S3 API)
                              │
                           MinIO ── put/delete event ──▶ Kafka file-upload-events
                              ▲                                   │
                     download │                                   ▼
 browser ──▶ frontend (Next.js) ──▶ backend (Go) ◀──┐        ingestion ──▶ Tika (text extraction)
                                       │    │      │            │
                         Redis cache ◀─┘    │      │            ├──▶ model-server (embed)
                                            │      │            └──▶ OpenSearch (index)
                            OpenSearch ◀────┘      │                 ▲
                         (hybrid query)            │ gRPC            │ failures
                                                   ▼                 ▼
                                             search-worker ──▶ model-server     Kafka -dlq ──▶ dlq-retry
                                          (parse, embed query,  (FastAPI: all          ▲          │
                                           rerank, answer)       models)               └── re-send ┘
                                                                                     (≤3 times, then -dlq-failed)
```

| Service | Language | Role |
|---|---|---|
| `workers/model-server` | Python, FastAPI | The only process that loads models: text embedder, image (CLIP) embedder, cross-encoder reranker, optional answer LLM. CUDA if visible, else CPU. |
| `workers/ingestion` | Python | Kafka consumer: MinIO event → download → Tika → chunk → embed → OpenSearch. Images get a CLIP vector plus filename/EXIF text. |
| `workers/ingestion` (`dlq_retry_worker.py`) | Python | Re-sends dead-lettered events to the main topic, at most 3 times, then parks them on `-dlq-failed`. |
| `workers/search` | Python, gRPC | `ParseQuery` (spaCy filters + keywords, query vectors), `Rerank`, `Answer`. Calls the model server; never touches OpenSearch. |
| `backend` | Go | HTTP gateway: builds and runs the hybrid OpenSearch query, dedups, reranks, applies cut-offs, caches in Redis. |
| `frontend` | Next.js (JS) | Chat-style UI. Its `/api/*` routes proxy to the backend and stream downloads from MinIO. |
| MinIO, Kafka (1 broker, KRaft), Tika, OpenSearch 2.19, Redis | — | Backing services. Kafka topics: `file-upload-events`, `-dlq`, `-dlq-failed`. |

The gRPC contract is `shared/proto/search.proto`; Python and Go stubs are generated at image build.
Model names and vector dimensions live only in `workers/model-server/model_spec.json`: the model
server loads from it and `infrastructure/opensearch/init.py` renders the index mapping from it.

## Ingestion

1. An upload to bucket `uploads` makes MinIO publish an S3 event to Kafka (deletes too, so removed
   files leave the index).
2. The ingestion worker handles messages one at a time per partition and commits an offset only
   after the message is indexed or dead-lettered, so a crash means redelivery, never loss.
3. Text files: Tika extracts the text; it is split into ~512-character overlapping chunks, each
   embedded (`text_embedding`) and indexed with filename, size, MIME type and `uploaded_at` (the
   event time). Images: one document with `image_embedding` and filename/EXIF text. Files with no
   extractable text are still indexed by name and metadata.
4. Re-indexing an object first deletes its old chunks. Oversized files (> 100 MB), 4xx errors and
   any failed bulk item send the message to the DLQ with a reason; 5xx/429 are retried.

## Search

`GET /search?q=…&limit=…` (limit clamped 1–50):

1. **Cache.** Key = hash of normalised query + limit, 30 s TTL. Empty and error responses are not
   cached; search works with Redis down.
2. **Parse.** The search worker strips filter phrases (extension, MIME family, size, date range in
   a configured timezone), returns lemmatised keywords for BM25, and embeds the remaining natural
   query with both the text model and the CLIP text encoder.
3. **Retrieve.** Go sends one hybrid query with three legs — BM25 on text, kNN on
   `text_embedding`, kNN on `image_embedding` — with the **same filter on every leg**, fused by an
   RRF search pipeline. It fetches up to 3× `limit` (max 50) candidates and dedups by `object_key`
   in Go (OpenSearch ignores `collapse` on hybrid queries).
4. **Rerank.** Text hits: the cross-encoder scores up to three passages per file; a file takes its
   best passage's score, that passage becomes the snippet, and its best sentence is highlighted.
   Image hits keep their raw cosine score.
5. **Cut-off and merge.** Both cut-offs are absolute (`RERANK_MIN_SCORE`, default -7.0;
   `IMAGE_MIN_SCORE`, default 0.24). The two lists are interleaved, text first unless its best
   hit scores under 0 and an image matched. If the reranker fails, the fused order is returned.

`GET /answer?q=…` is a separate call the UI makes after results arrive. It writes an answer from
the top 3 text results, citing their filenames, and is available only when the model server loaded
the answer model (`ANSWER_ENABLED=1` and a CUDA device). Otherwise the UI hides the panel.

`GET /health` pings OpenSearch and the search worker (not Redis, which is optional); it never
runs a search.

## Deployment

The same images and the same `.env` drive both targets.

- **Docker Compose** (`infrastructure/docker-compose.yml`): `make up`. Ports are published on
  127.0.0.1 only, since the backing services run without authentication. One-shot containers create
  the Kafka topics, the bucket and its event rule, and the index and search pipeline.
- **minikube** (`infrastructure/k8s/`): `bash infrastructure/k8s/deploy.sh`. Images are built into
  minikube's Docker; credentials go into a Secret built from `.env`; the same setup scripts run as
  Jobs; services are ClusterIP only (use `kubectl port-forward`).
- **GPU** is opt-in with `GPU=1` on either target: the model server image gets CUDA PyTorch and
  the container gets the GPU. Without it everything runs on CPU.

## Measured memory

Idle after a smoke test, 2026-11-13, minikube (`docker stats` inside the node). Compose runs the
same containers.

| Service | RAM (CPU) | RAM (GPU) | k8s request / limit |
|---|---|---|---|
| opensearch | 1.43 GiB | 1.48 GiB | 1.5 / 2 GiB |
| model-server | 1.0 GiB | 1.23 GiB + 926 MiB VRAM | 2 / 3 GiB |
| kafka | 330 MiB | | 768 MiB / 1 GiB |
| tika | 187 MiB | | 768 MiB / 1.5 GiB |
| search-worker | 85 MiB | | 256 / 512 MiB |
| minio | 76 MiB | | 256 / 512 MiB |
| ingestion | 69 MiB | | 256 / 512 MiB |
| frontend | 53 MiB | | 256 / 512 MiB |
| dlq-retry | 18 MiB | | 128 / 256 MiB |
| backend | 13 MiB | | 64 / 256 MiB |
| redis | 4 MiB | | 64 / 320 MiB |
| **total** | **≈ 3.3 GiB** | | 6.3 GiB requests |
| Kubernetes system pods | ≈ 0.45 GiB | | |

Tika and OpenSearch grow under load (JVM heaps capped at 1 GiB each; Kafka at 512 MiB). The
answer model is not included: it loads only with `ANSWER_ENABLED=1` on a GPU.

## Search quality

Measured with `make eval` on 41 text and 9 image queries (`docs/model-evaluation.md`): text
recall@5 0.927 / MRR 0.915, image recall@5 1.000 / MRR 1.000. Model choices and cut-off evidence
are recorded there.
