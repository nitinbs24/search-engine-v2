# Codebase Analysis Report

Date: 2026-11-11 · Commit analysed: `ed7715e` (master) · Method: static read of every tracked source file. **Nothing was run** — the stack was not started, so findings marked *(verify)* need a live check.

## 1. Summary

The architecture (MinIO → Kafka → ingestion worker → OpenSearch; Next.js → Go gateway → Redis → gRPC Python worker → OpenSearch) is sound and worth keeping. The problems are in three places:

1. **Search relevance is undermined by bugs, not just by the model.** Filters do not apply to the vector half of the query, the query is reduced to a bag of lemmas before embedding, the reranker's ordering is thrown away by the gateway, and the score cutoff is applied to scores that are relative to each result set. A smaller model will work acceptably only after these are fixed.
2. **The repo is two projects stacked on top of each other**, plus about 1,500 lines of dead code and four generations of stale configuration (MiniLM 384-dim → nomic → CLIP ViT-B/32 → Jina CLIP v2).
3. **It is too heavy for the target laptop** (15 GB RAM, GTX 1650 Ti 4 GB): four ML models, two of them the same 0.9B-parameter model loaded twice, three Kafka brokers, and a Kubernetes manifest that cannot schedule without a GPU.

## 2. What actually runs

| Component | Location | Language | Notes |
|---|---|---|---|
| Ingestion worker | `workers/ingestion/` | Python 3.11 | Kafka consumer → Tika → chunk (600 chars) → Jina CLIP v2 → OpenSearch |
| DLQ retry worker | `workers/ingestion/dlq_retry_worker.py` | Python | Deployed on k8s only, not in compose |
| Search worker ("pyworker-2") | `Search-Engine/pyworker/` | Python 3.12 | gRPC; spaCy parse → Jina CLIP v2 → hybrid query → cross-encoder rerank → Flan-T5 summary |
| Gateway | `Search-Engine/gateway/` | Go 1.23 | REST `/search`, `/health`; Redis cache |
| Frontend | `Search-Engine/frontend/` | Next.js 14.2.5 | Chat-style UI, `/api/search` proxy, `/api/download` MinIO proxy |
| Infra | `docker-compose.yml`, `k8s/` | — | Kafka ×3 (KRaft), MinIO, Tika, OpenSearch 2.19.1 + Dashboards, Redis |

Models loaded at runtime: `jinaai/jina-clip-v2` (in **both** workers), `cross-encoder/ms-marco-MiniLM-L-6-v2`, `google/flan-t5-small`, spaCy `en_core_web_sm`.

## 3. Structural problems

### 3.1 Split layout
- The search half lives in `Search-Engine/` with its own `README.md`, `.env.example`, `.gitignore`, `docker-compose.yml` and `Architecture/`; the ingestion half lives at the root in `workers/`, `infrastructure/`, `k8s/`. The root compose and `k8s/deploy.sh` reach into `Search-Engine/` as a build context.
- `CLAUDE.md` describes a third project that no longer exists (`api/api.py`, `indexer/indexer.py`, index `object-storage-index`, podman, port 8000).

### 3.2 Dead code (safe to delete)
| Path | Lines | Why dead |
|---|---|---|
| `workers/model-server/` | ~600 | **Unused today, but to be revived** (decision 2026-11-11: follow the original planned layout, where one model server serves both workers). Currently replaced by in-process Jina models; still built and started by root `docker-compose.yml`, loading two obsolete models that nothing calls. |
| `backend/` | ~700 | Python cache + query builder. Imported only by tests. The gateway and pyworker have their own implementations. |
| `Search-Engine/docker-compose.yml` | 131 | Superseded by root compose. |
| `infrastructure/kafka/*.yaml`, `topics.sh`, `dead-letter/dlq-consumer.py`, `minio/`, `startup.sh` | — | Strimzi-era setup; `k8s/` uses a plain StatefulSet. `dlq-consumer.py` imports `kafka-python`, which is in no requirements file. |
| `search_worker.py` `_keyword_search`, `_keyword_search_opensearch` | ~85 | Never called. |
| `frontend/app/api/search/route.js` `parseNaturalQuery`, `monthToIndex` | ~85 | Never called. |
| `frontend/next.config.js` | — | Duplicate of `next.config.mjs`. |
| Root `requirements.txt` | — | No Dockerfile uses it; pulls FastAPI/uvicorn/structlog that nothing imports. |
| Empty placeholders | — | `shared/`, `workers/search/`, `backend/api|config|grpc`, `infrastructure/redis`. |
| `Claude_resume.txt`, `Search-Engine/Architecture/hpet`, `role4_5_plan.md` | — | Session leftovers and old plans with another machine's absolute paths. |

### 3.3 Duplication and drift
- **Embedding code is copy-pasted** between `workers/ingestion/model_client.py` and `Search-Engine/pyworker/embedding_service.py`. They have already drifted (`batch_size=8` in one only). Query and document embeddings must come from identical code; this is a correctness risk, not just tidiness.
- **Proto drift**: `pyworker/proto/search.proto` has `rerank_score = 13`; `gateway/proto/search.proto` does not. The gateway Dockerfile regenerates stubs from its own copy.
- **Config drift**: model name/dimension appears as MiniLM-L6 384 (`Search-Engine/.env.example`, compose, `model_spec.json`, `chunker.py` and `opensearch_client.py` docstrings), `clip-ViT-B-32` (`pyworker/config.py`, k8s ConfigMap), and Jina CLIP v2 (actual code, hard-coded). `EMBEDDING_MODEL` env is read and ignored.
- Kafka group id: `reset-index.sh` resets `ingestion-worker-group`; the worker uses `ingestion-worker`. The reset silently does nothing.
- Docs contradict code: `TECHNICAL_DOCUMENTATION.md` claims SHA-256 document IDs, Zookeeper, and gateway-enforced RBAC. None of these exist.

## 4. Search-quality bugs (most important section)

| # | Bug | Where | Effect |
|---|---|---|---|
| S1 | **Filters are attached only to the BM25 sub-query; the kNN sub-query has none.** | `search_worker.py:236-264` | "pdfs about budgets" returns non-PDFs via the vector side. Every type/date/size filter leaks. |
| S2 | **The reranker's order is discarded.** The gateway proto lacks `rerank_score` and `merger.Merge` re-sorts by `combined_score`. | `gateway/merger/merger.go:76`, gateway proto | The cross-encoder costs latency and changes nothing except which result gets the AI summary — which is then not necessarily shown first. |
| S3 | **Query is embedded as a bag of lemmas.** Only NOUN/PROPN/ADJ/NUM lemmas survive; verbs and word order are dropped. | `nlp_parser.py:_spacy_parse` | Sentence-embedding models are trained on natural text; this degrades semantic recall noticeably. |
| S4 | **`in <word>` / `from <word>` are treated as month filters and stripped.** "errors in python", "emails from john" lose "python"/"john" entirely; the resulting `month:` filter is then ignored. | `nlp_parser.py:22-23` | Query terms silently vanish. |
| S5 | **Fixed 0.45 cutoff on min-max-normalised scores.** Normalised scores are relative to the current result set, so the cutoff is not a relevance threshold. | `search_worker.py:334` | Arbitrary drops; weak result sets still have a "1.0". |
| S6 | "documents"/"docs" becomes a filter `mime_type` prefix `application/`. | `search_worker.py:443` | Excludes `.txt`; includes zip/json. |
| S7 | `collapse` on `filename.keyword`. | `search_worker.py:283` | **Verified: OpenSearch 2.19.1 silently ignores `collapse` on a hybrid query** — duplicates come back. The Python loop after it is what actually deduplicates, and it keys on filename only, so same-named files in different buckets merge. |
| S8 | `\.(\w+)` treats any dotted token as an extension ("v2.0", "node.js"). | `nlp_parser.py` | Bogus filters → zero results. |
| S9 | Date handling: "today" = last 24 h, naive `datetime.now()` combined with a hard-coded `+05:30` zone; unknown month defaults to January. `uploaded_at` is ingestion time, not the object's time. | `nlp_parser.py:get_date_range`, `main.py:216` | Off-by-hours date filters; re-indexing changes every date. |
| S10 | `semantic_score`/`keyword_score` are the same combined number relabelled. | `search_worker.py:313` | UI shows fabricated breakdown. |
| S11 | Summary prompt is hard-coded to "legal terminology"; Flan-T5-small on one 600-char chunk. | `search_worker.py:76` | Low-value output for the cost. |
| S12 | Embedding failure returns a zero vector instead of an error. | `embedding_service.py:84` | Cosine kNN on a zero vector → garbage or an OpenSearch error, cached for 5 min. |

Index mapping (`infrastructure/opensearch/index-mapping.json`): engine `nmslib` is deprecated in 2.19 and does not support efficient filtering (needed to fix S1 properly — use `lucene` or `faiss`); `number_of_replicas: 1` on a single node keeps the index permanently yellow; `ef_search`/`ef_construction` of 512 are oversized for a laptop corpus.

## 5. Gateway and frontend bugs

- **Cache key is the raw query only** (`cache/redis.go:Key`). `limit` is not part of it, so `limit=3` then `limit=20` returns 3. Empty and degraded results are cached too.
- **`/health` runs a full search** (parse, embed, kNN, rerank, summarise) and is wired as the k8s readiness probe.
- **No RBAC anywhere.** The three role buckets exist and the bucket is indexed, but no request carries a role and no query filters on bucket.
- **`/api/download` is an unauthenticated proxy to any bucket/key using MinIO root credentials.**
- **Download uses `object_name` (basename) as the key**, so any file uploaded under a prefix (`reports/q1.pdf`) cannot be downloaded or previewed.
- CORS `*`; gRPC and all backing services unauthenticated; default credentials committed in compose and ConfigMap (acceptable for a local demo, worth a Secret on k8s).
- Next.js 14.2.5 is behind several published security patches; no lockfile is committed, so builds are not reproducible. The Dockerfile sets `output: standalone` but copies full `node_modules` anyway.
- There is no upload UI; files go in through `mc`, the MinIO console, or `infrastructure/upload.sh`.

## 6. Ingestion bugs

| # | Bug | Where |
|---|---|---|
| I1 | **Oversized files are silently dropped.** The comment says "caller will publish to DLQ"; the code just `return`s (also skipping remaining records in the event). | `main.py:189-194` |
| I2 | **Offsets are committed out of order.** Up to N messages are processed concurrently and each commits its own offset; committing a later offset acknowledges earlier in-flight ones. A crash loses them. Task creation is also unbounded — the semaphore limits work, not queued tasks. | `main.py:411-469` |
| I3 | **Stale chunks on re-upload.** IDs are `key#index` with upsert; a new version with fewer chunks leaves the old tail in the index. Bucket versioning is on but versions are not modelled. | `opensearch_client.py`, `main.py` |
| I4 | **Bulk failures are not failures.** `bulk_upsert` returns counts; the caller logs and commits. Partially indexed files never reach the DLQ. | `main.py:390` |
| I5 | **DLQ retry loop is wrong.** The retry worker re-queues the original event *and* writes an updated envelope back to the DLQ, which it then consumes itself — so every failure is retried 3 times even if the first retry succeeded, and each real re-failure creates a fresh envelope with `retry_count` 0. It also sleeps up to 900 s inside the consumer, past Kafka's default 300 s poll interval. | `dlq_retry_worker.py:214-285` |
| I6 | Blocking MinIO download inside the async loop; whole file held in memory. | `main.py:199` |
| I7 | `_with_retry` retries every `TransportError`, including permanent 4xx mapping errors. | `opensearch_client.py` |
| I8 | Images are squashed to 224×224 JPEG before embedding (sized for the old model; Jina CLIP v2 expects 512). Images are written to a temp file to embed. | `image_handler.py:283`, `model_client.py` |
| I9 | Text files with no extractable text, and files with no text layer (scanned PDFs), are skipped with a warning — not searchable even by filename. | `main.py:332` |

## 7. Deployment

**Laptop:** 15 GB RAM, 12 threads, GTX 1650 Ti (4 GB VRAM). Docker and podman installed; **minikube, kubectl and Go are not installed.**

- `k8s/search/pyworker.yaml` sets `nvidia.com/gpu: 1` unconditionally → pod stays Pending on any cluster without the NVIDIA device plugin. Memory limit 10 Gi.
- `deploy.sh` starts minikube with `--memory=12288` — most of the machine.
- Three Kafka brokers with replication factor 3 (≈1.5–3 GB) for a single-node demo.
- Compose still runs the dead model-server, has no DLQ retry worker, and mounts the Docker socket into `willfarrell/autoheal:latest`.
- Unpinned images: `apache/tika:latest`, `minio/mc:latest`, `curlimages/curl:latest`, `autoheal:latest`. Python requirements are mostly unpinned (`torch`, `grpcio`, `spacy`).
- Compose and k8s configuration are maintained by hand in parallel and have diverged (model-server, DLQ worker, env names, service name `pyworker-2` vs `pyworker`).
- ingestion-worker and pyworker share one ReadWriteOnce PVC; `hf-cache-pyworker` PVC is unused.
- No CI, no Makefile, no lint config.

Rough current memory footprint (estimates, not measured): Kafka ×3 ~2 GB, OpenSearch ~1.5–2 GB, Dashboards ~0.5 GB, Tika ~1 GB, Jina CLIP v2 ×2 ~4–7 GB, reranker + T5 + spaCy ~1 GB, model-server (dead) ~1–2 GB. This does not fit comfortably in 15 GB alongside a desktop.

## 8. Tests

- `tests/test_opensearch.py` (28 tests) tests the dead `backend/` package.
- `tests/test_features_2_and_7.py` (16) tests ingestion with mocks — likely still useful *(verify it passes)*.
- `workers/ingestion/tests/test_image_search_effectiveness.py` (36) calls `ModelClient(model_server_url=...)` against the removed model-server.
- `tests/test_hybrid.py` and four files in `workers/ingestion/tests/` contain zero tests; they are scripts named `test_*`.
- **No tests at all** for the NLP parser, the search worker's query building, the Go gateway, or the frontend — exactly where the bugs in §4–5 are.

## 8a. Verification results (2026-11-11)

Run against a throwaway OpenSearch **2.19.1** container (podman) with a six-document index, and the old ingestion tests in a Python 3.11 container. Both removed afterwards.

| Claim | Result |
|---|---|
| S1: filters leak through the kNN leg | **Confirmed.** With `extension:pdf` on the BM25 leg only, the query returned txt, png, docx and jpg documents. |
| Per-leg filters fix it on 2.19 | **Confirmed.** `knn.<field>.filter` on each vector leg + `bool.filter` on BM25 returned only PDFs. |
| A top-level `hybrid.filter` could be used instead | **No.** Rejected on 2.19.1 ("Field is not supported by [hybrid] query"); that is a 3.0 feature. |
| Three-leg hybrid (BM25 + two different kNN fields) works | **Confirmed.** Documents missing one of the vector fields are fine. Limit is 5 sub-queries; the pipeline's weight count must equal the number of legs. |
| `nmslib` cannot do filtered kNN | **Confirmed** ("Engine [NMSLIB] does not support filters"). Index creation with nmslib still succeeds on 2.19.1; it is blocked from 3.0. |
| S5: min-max scores are not a relevance measure | **Confirmed.** The document that was both the exact vector match and a keyword match ranked third, and documents with a combined score of 0.0 were still returned. |
| Raw kNN scores are absolute | **Confirmed.** Lucene cosine scores are `(1+cos)/2` regardless of the other hits (orthogonal = 0.5). |
| S7: `collapse` + hybrid | **Corrected** — not an error; silently ignored (see S7). |
| RRF is available as an alternative to min-max | **Confirmed.** `score-ranker-processor` with `rrf` is accepted on 2.19.1. |
| `tests/test_features_2_and_7.py` still passes | **13 of 16 pass.** Two need a live OpenSearch. One real failure: `test_empty_metadata_still_serialises`. |

Environment findings from the same session:

- **Root/home partition is 89% full (11 GB free).** OpenSearch refused to create an index until its disk-threshold check was disabled; its high watermark is 90%. The full image set plus model downloads will not fit comfortably.
- **`/mnt/CODE` is NTFS (fuse).** Fine for source, but symlinks and Unix permissions are unreliable there, and container data must not live on it.
- **Docker is Docker Desktop**, not a native engine (only the `desktop-linux` context has a daemon; no `docker.service`). Docker Desktop on Linux runs containers in a VM, which — as far as I know — cannot pass through the NVIDIA GPU. GPU in containers needs native Docker Engine + NVIDIA Container Toolkit. *(Not tested: Docker Desktop was not running.)*

## 9. What is good and should be kept

- The event-driven ingestion design, manual offset commits, and DLQ concept.
- Deterministic chunk IDs and delete-on-`ObjectRemoved`.
- Native OpenSearch hybrid query with a normalisation pipeline (correct approach; just needs filters on both legs).
- Go gateway is small, readable, and degrades gracefully without Redis.
- `image_handler.py` EXIF extraction feeding BM25 text for images.
- Server-side download proxy (right idea; needs a key fix and access control).
- k8s init-job ordering and `deploy.sh` GPU fallback logic.

## 10. Lower-tier model: options

Constraint that matters: the embedding model is used for **text→text and text→image** in one 512-dim field. Changing model or dimension means re-creating the index and re-ingesting everything.

| Option | Models | Trade-off |
|---|---|---|
| **A. Smaller multimodal model, same design** (recommended starting point) | e.g. `jinaai/jina-clip-v1` (~0.2B params, 768-dim, English) | One model, one vector field, minimal code change. ~4× smaller than v2. English only. Quality to be measured, not assumed. |
| B. Two models, two vector fields | small text model (e.g. `bge-small-en-v1.5`, 384-dim) + small CLIP for images | Best text quality per MB; more code and a three-leg hybrid query. |
| C. Text-only | small text model; images searchable by filename/EXIF only | Lightest; drops image semantic search. |

Independent of the choice: load the model **once** (one process serving both ingestion and query embedding, or one shared module), make the model name and dimension real configuration, and decide whether the reranker and Flan-T5 summary are worth keeping (I would keep the reranker after fixing S2 and make the summary optional).

Note: `jina-clip-v2` is licensed CC BY-NC 4.0 (non-commercial) — fine for a campus project, relevant if this goes anywhere else.

## 11. Suggested target layout (for discussion)

```
services/
  ingestion/      # from workers/ingestion
  search/         # from Search-Engine/pyworker
  gateway/        # from Search-Engine/gateway
  frontend/       # from Search-Engine/frontend
libs/embedding/   # single shared embedding module
proto/            # single search.proto, stubs generated at build
deploy/
  compose/        # docker-compose.yml
  k8s/            # manifests + scripts
  opensearch/     # index mapping, search pipeline
tests/
docs/
```

## 12. Open questions (need your answer before planning)

1. Model option A, B or C? Is image semantic search a must-have? English only?
2. Should the model run on the 1650 Ti (4 GB) or CPU-only so it works on any laptop?
3. Is RBAC (role buckets) in scope — real enforcement with login, a simple role selector, or dropped?
4. Keep the AI summary (Flan-T5)? Keep the cross-encoder reranker?
5. Kafka: is 1 broker acceptable locally (3 as an optional profile), or must it stay 3?
6. "Docker" — Docker only, or must podman-compose keep working too?
7. Should the frontend get an upload feature, or stay search-only?
8. Is there existing indexed data to preserve, or is a clean re-index fine?
9. Do the other team members' commits/history need preserving in a particular way (branch vs. in-place on master)?
