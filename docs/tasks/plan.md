# Implementation Plan: Search Engine Rebuild (new repo, Obsidian layout)

Status: **draft — awaiting owner approval.** Inputs: `docs/intent/refactor.md`, `docs/ANALYSIS_REPORT.md`.
Task checklist: `docs/tasks/todo.md`. (Kept under `docs/` at the owner's request; `/agent-skills:build` expects `tasks/todo.md`, so point it at this path.)

## Overview

Build a new repository in the planned layout by carrying over the code worth keeping from the old repo ("old" = `/mnt/CODE/Search Engine`) and rewriting the parts that were wrong. One model server holds all models; Go runs the hybrid query; the Python search worker only parses and embeds. Text and image search must both work on small models, on Docker Compose and minikube, on a 15 GB laptop.

## Architecture decisions

1. **Two embedding models, two vector fields.** `text_embedding` (small text model, ~384-dim) on document chunks; `image_embedding` (small CLIP-family model, ~512-dim) on images. A query is embedded by both the text model and the CLIP text encoder. Reason: best document quality per MB, and it matches the planned `text_embedder.py` / `image_embedder.py` split. Exact models are chosen in Task 9 by measurement.
2. **`workers/model-server/model_spec.json` is the single source of truth** for model names and dimensions. The model server reads it; the index mapping is rendered from it; nothing else hard-codes a model or a dimension.
3. **Model server endpoints:** `POST /embed/text`, `POST /embed/query` (returns both vectors), `POST /embed/image`, `POST /rerank`, `POST /answer` (optional), `GET /health`. Device: CUDA if visible, else CPU.
4. **gRPC contract (`shared/proto/search.proto`):** `ParseQuery(query) → {keywords, filters, text_vector, image_vector}` and `Rerank(query, passages) → {scores, best_sentences}`. Go never calls the model server directly; it goes through the search worker, as the layout specifies. Stubs are generated at image build, not hand-copied.
5. **Go owns the query.** `backend/search/opensearch.go` builds one hybrid query with three legs — BM25, kNN on `text_embedding`, kNN on `image_embedding` — and **the same filter on every leg**. kNN engine `lucene` (supports filtered kNN; `nmslib` is deprecated). Deduplication by `object_key` happens in Go, not with `collapse`.
6. **Relevance cut-off uses absolute scores** (reranker score for text hits, raw cosine for image hits), never min-max-normalised ones.
7. **Final order is the reranker's order.** Text hits are reranked; image hits keep their vector score. How the two are interleaved is tuned against the evaluation set (Task 9/13).
8. **Parser changes:** the natural query string is embedded as typed (minus recognised filter phrases); lemmatised keywords are used only for BM25. Month filters require a real month name. Dotted tokens are extensions only if in the known list.
9. **Ingestion:** `uploaded_at` is the object's event time. Re-ingesting an object deletes its old chunks first. Any indexing failure goes to the DLQ. Messages are processed in order per partition, so offset commits are safe.
10. **Health endpoints are cheap** (dependency pings), never a full search.
11. **Cache key** = hash of normalised query + limit. Empty and error responses are not cached.
12. **Kafka: one broker** in both Compose and minikube.
13. **Summary panel:** best sentences from `Rerank` come back with results; `GET /answer` is a separate call the UI makes afterwards, enabled only when the model server reports an answer model loaded.

## Dependency graph

```
shared/proto + model_spec.json
        │
        ├── model-server ──────────────┐
        │        │                     │
        │        ├── ingestion ── index mapping ── OpenSearch
        │        │                     │
        │        └── search worker ────┤
        │                 │            │
        └──────────── Go backend ──────┘
                          │
                       frontend
infrastructure (compose) underlies everything; k8s mirrors compose last.
```

## Phases

Each task is done test-first (`/agent-skills:test`): write the failing test, make it pass, commit. Tasks are sized S–M (≤5 files).

### Phase 0 — Bootstrap
- **T1** Create the new repo with the layout skeleton, `.gitignore`, `.env.example`, `Makefile` (`up`, `down`, `test`, `smoke`).
- **T2** `infrastructure/docker-compose.yml` with backing services only: MinIO + init, Kafka (1 broker) + topic init, Tika, OpenSearch, Redis. All images pinned. `make up` ends with every service healthy.

### Phase 1 — Walking skeleton: a text file is searchable end to end
- **T3** Model server, text only: `/embed/text`, `/embed/query`, `/health`, driven by `model_spec.json`. Start with `all-MiniLM-L6-v2` as a placeholder.
- **T4** Index mapping rendered from `model_spec.json` + OpenSearch init (index, hybrid search pipeline).
- **T5** Ingestion, text path: carry over `main.py`, `tika_extractor.py`, `chunker.py`, `opensearch_client.py`; `model_client.py` becomes an HTTP client. Upload a `.txt` → chunks in the index.
- **T6** `shared/proto/search.proto` + search worker `ParseQuery` (parser carried over as-is for now).
- **T7** Go backend: config, gRPC client, hybrid query (BM25 + text kNN, filters on both legs), `/search`, cheap `/health`.
- **T8** Frontend moved to `frontend/`, single config file, lockfile committed, download uses the full object key. `make smoke` uploads a file and finds it through the UI's API route.

**Checkpoint A:** `make up && make smoke` passes on Compose · human review · **CodeRabbit review**.

### Phase 2 — Search quality
- **T9** Evaluation set + harness: ~30 documents and images, ~40 queries with expected results, a script printing recall@5 and MRR. Run it against 2–3 text-model candidates and 2 image-model candidates; record results in `docs/model-evaluation.md`; fix the choice in `model_spec.json`.
- **T10** Parser fixes (decision 8) with a table-driven test per bug S3, S4, S6, S8, S9.
- **T11** Reranker: `/rerank` on the model server, `Rerank` RPC, Go applies reranker order and absolute cut-off; dedup by `object_key`.
- **T12** Redis cache carried over with the corrected key and no caching of empty/error responses.

**Checkpoint B:** evaluation numbers recorded and not worse than Checkpoint A baseline · human review.

### Phase 3 — Images
- **T13** Image embedder + `/embed/image`; `image_handler.py` carried over with the resize matched to the chosen model; images indexed with `image_embedding` and their EXIF/filename text.
- **T14** Third query leg (kNN on `image_embedding`) + interleaving rule, tuned on the evaluation set; frontend preview still works.

**Checkpoint C:** text and image queries both pass the evaluation thresholds · human review · **CodeRabbit review**.

### Phase 4 — Ingestion reliability
- **T15** Oversized/unsupported/empty files: DLQ or filename-only indexing, never silently dropped (I1, I9).
- **T16** Ordered processing and safe commits; streamed download (I2, I6).
- **T17** Delete-before-reindex; bulk failures raise; retry only on transient errors (I3, I4, I7).
- **T18** DLQ retry worker rewritten: retry count carried in a Kafka header, no self-consumption, no long sleeps inside the poll loop (I5). Added to Compose.

**Checkpoint D:** fault-injection tests pass (kill worker mid-file, re-upload shorter file, Tika down) · human review.

### Phase 5 — Summary panel
- **T19** Extracted passage: `Rerank` returns best sentences; UI shows them highlighted in place of the old AI summary.
- **T20** Generated answer: optional answer model on the model server (loaded only if enabled and a GPU is visible), `/answer` in Go, UI fetches it after results. Disabled → UI shows T19 output only.

**Checkpoint E:** works with the answer model on and off · human review · **CodeRabbit review**.

### Phase 6 — minikube
- **T21** `infrastructure/k8s/` manifests mirroring Compose (one broker, Secrets for credentials, no hard GPU requirement, cheap probes) + `deploy.sh`.
- **T22** Optional GPU overlay for the model server on both Compose and minikube; documented prerequisite: NVIDIA Container Toolkit.

**Checkpoint F:** `make smoke` passes against minikube · human review.

### Phase 7 — Finish
- **T23** `docs/architecture.md`, `docs/setup.md`, README; measured memory footprint recorded.
- **T24** Final pass: no hard-coded model names/dimensions outside `model_spec.json`, no unused files, all tests green.

**Checkpoint G:** all success criteria in the intent doc met · **CodeRabbit review** · done.

## What is carried over vs rewritten

| Carried over (with fixes) | Rewritten | Left behind |
|---|---|---|
| `tika_extractor.py`, `chunker.py`, `image_handler.py`, `opensearch_client.py`, ingestion `main.py` | Hybrid query (Python → Go) | `backend/` (Python), `Search-Engine/docker-compose.yml` |
| `nlp_parser.py` | DLQ retry worker | Strimzi YAMLs, `dlq-consumer.py`, `startup.sh` |
| Go `cache/`, gRPC client, `main.go` | Model server internals (new models, new endpoints) | Flan-T5, in-process Jina loading |
| Frontend `page.js`, `globals.css`, download route | k8s manifests | Old tests for dead code, `Claude_resume.txt`, `hpet` |
| `tests/test_features_2_and_7.py` (if it still passes) | | Three-broker Kafka, `autoheal` |

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Small models give poor results | High | Evaluation set (T9) before tuning anything; model is one line in `model_spec.json`. |
| ~~Three-leg hybrid query / filtered kNN behaves differently on OpenSearch 2.19 than assumed~~ | — | Verified on 2.19.1 (see below). |
| ~~Disk: `/` at 89%, OpenSearch watermark at 90%~~ | — | Resolved: container storage moved to a dedicated 100 GB partition. |
| Repo lives on NTFS: symlinks and file modes unreliable | Medium | No symlinks (the layout's two symlinks become a build-time copy); scripts invoked as `bash script.sh`; named volumes only, no data bind-mounts. |
| ~~Docker Desktop VM cannot use the GPU~~ | — | Resolved: native engine + NVIDIA toolkit installed; GPU stays opt-in. |
| Mixing reranked text hits with image hits ranks badly | Medium | Tuned and measured in T14; simplest fallback is separate "Documents" and "Images" groups in the response. |
| Answer LLM doesn't fit in 4 GB with the other models, or is poor | Medium | It is last, optional, and behind a flag; T19 stands alone. |
| A Python folder named `grpc/` shadows the `grpc` package | High (import breaks) | See open question 3. |
| Host lacks Go, minikube, kubectl; Docker daemon not running; no NVIDIA Container Toolkit | Medium | Go is built and tested inside a container. The rest are owner installs, needed before Phase 6 (toolkit only for T22). |
| Compose and k8s drift apart again | Medium | Both read the same `.env`-style config; `make smoke` runs against either. |

## Resolved (owner, 2026-11-11)

1. New repo: `/mnt/CODE/search-engine-v2` (does not exist yet; distinct from the old `/mnt/CODE/Search Engine`).
2. Fresh git history; first commit references old commit `ed7715e`.
3. `workers/search/grpc/` → `workers/search/proto_gen/` (accepted deviation).
4. English only.
5. Evaluation set assembled from public, freely licensed documents and images.

## Verified before coding (details in `ANALYSIS_REPORT.md` §8a)

- Decision 5 holds on OpenSearch 2.19.1: three-leg hybrid with a filter on each leg works; the filter must be per leg (`knn.<field>.filter`), a top-level `hybrid.filter` is rejected.
- `collapse` is silently ignored on hybrid queries → dedup in Go is required, not optional.
- Decision 6 holds: min-max scores misrank and never drop anything; raw Lucene cosine scores are absolute.
- The pipeline's weight count must equal the number of legs → the pipeline is created by the same code path that defines the legs (T4/T14), and a test asserts they match.
- RRF (`score-ranker-processor`) is available on 2.19.1 → evaluated against min-max in T9/T14.

## Environment (set up and verified 2026-11-11)

- Native Docker Engine 29.9.3 (Docker Desktop removed). Its data lives on a dedicated 100 GB ext4 partition on the hard disk (`/dev/sda4`, bind-mounted at `/var/lib/docker` and `/var/lib/containerd`); Docker refuses to start if it is not mounted. `/` is at 72% (28 GB free).
- NVIDIA Container Toolkit 1.20.2: `docker run --gpus all` sees the GTX 1650 Ti.
- Container storage is on a spinning disk: expect slow image pulls, OpenSearch start-up and model loads.
- Still to install before Phase 6: minikube, kubectl.

## Open questions (need the owner)

None. Waiting for the go-ahead to start T1.
