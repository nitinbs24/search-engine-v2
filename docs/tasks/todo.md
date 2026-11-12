# Task List — Search Engine Rebuild

Plan: `docs/tasks/plan.md`. Every task is test-first. Paths are relative to the **new** repo. "Old" = `/mnt/CODE/Search Engine`.
Report bug IDs (S1, I5, …) refer to `docs/ANALYSIS_REPORT.md`.

## Phase 0 — Bootstrap

- [x] **T1 Repo skeleton** (S) — deps: open questions 1–3 answered
  - Accept: layout directories exist; `make help` lists `up down test smoke`; `.env.example` covers every variable used.
  - Verify: `git status` clean after first commit; `make help` runs.
- [x] **T2 Backing services on Compose** (M) — deps: T1
  - Accept: MinIO (+bucket `uploads`, Kafka event rule), Kafka ×1 (+`file-upload-events`, `-dlq`), Tika, OpenSearch, Redis; all image tags pinned; no Docker socket mounts.
  - Verify: `make up` → `docker compose ps` shows all healthy; uploading a file with `mc` produces one message on `file-upload-events`.
  - Files: `infrastructure/docker-compose.yml`, `infrastructure/minio/setup.sh`, `infrastructure/kafka/topics.sh`, `infrastructure/redis/redis.conf`.

## Phase 1 — Walking skeleton (text)

- [x] **T3 Model server: text** (M) — deps: T1
  - Accept: `/embed/text` returns one vector per input with the dimension in `model_spec.json`; `/embed/query` returns `text_vector`; `/health` reports loaded models and device; empty input → 422, not a zero vector (S12).
  - Verify: `pytest workers/model-server`; `curl /health`.
  - Files: `workers/model-server/{main,server,text_embedder}.py`, `model_spec.json`, `requirements.txt`, `Dockerfile`, tests.
- [x] **T4 Index mapping + init** (S) — deps: T2, T3
  - Accept: mapping generated from `model_spec.json`; engine `lucene`; 1 shard, 0 replicas; init is idempotent.
  - Verify: test asserts mapping dimension equals spec; cluster health green after init.
  - Files: `infrastructure/opensearch/{index-mapping.json or template, hybrid-plugin-setup.sh}`.
- [x] **T5 Ingestion: text path** (M) — deps: T3, T4
  - Accept: `.txt`/`.pdf` upload → chunks indexed with `text_embedding`; `uploaded_at` = event time; no torch/transformers in `requirements.txt`.
  - Verify: `pytest workers/ingestion` (mocked model server + OpenSearch); manual upload shows chunk count in the index.
  - Files: `workers/ingestion/{main,model_client,tika_extractor,chunker,opensearch_client}.py`.
- [x] **T6 Proto + search worker ParseQuery** (M) — deps: T3
  - Accept: one `search.proto` in `shared/proto/`; stubs generated at build for Python and Go; `ParseQuery` returns keywords, filters, `text_vector`.
  - Verify: `pytest workers/search` incl. an in-process gRPC round trip.
  - Files: `shared/proto/search.proto`, `workers/search/{main,nlp_parser,model_client}.py`, Dockerfile.
- [x] **T7 Go backend: /search** (M) — deps: T4, T6
  - Accept: hybrid query with BM25 + text kNN, **identical filter on both legs** (S1); `/health` pings dependencies only; limit clamped 1–50.
  - Verify: `go test ./...` (run in a `golang` container) with golden-JSON tests for the query builder; `curl /search?q=…` returns the T5 document.
  - Files: `backend/{main.go, api/routes.go, search/opensearch.go, grpc/client.go, config/config.go}`.
- [x] **T8 Frontend moved** (M) — deps: T7
  - Accept: current UI renders results from the Go backend; one Next config; lockfile committed; download/preview works for keys containing `/`; unused parser code removed; Next.js patched to latest 14.2.x.
  - Verify: `npm run build`; `make smoke` (upload → search via `/api/search` → download bytes match).
  - Files: `frontend/app/**`, `frontend/lib/api.js`, `frontend/Dockerfile`, `scripts/smoke.sh`.

### Checkpoint A
- [x] `make up && make smoke` green · all unit tests green
- [ ] Owner review
- [x] `/coderabbit:code-review` — 14 findings; 8 fixed, #1 → T11, #11 → T17, #13/#14 → T10, #8 and #9 left open

## Phase 2 — Search quality

- [ ] **T9 Evaluation set + model choice** (M) — deps: Checkpoint A, open questions 4–5
  - Accept: `eval/` holds documents, queries with expected results, and a runner printing recall@5 and MRR; results for each candidate in `docs/model-evaluation.md`; winners written to `model_spec.json`.
  - Verify: `make eval` reproduces the recorded numbers.
- [ ] **T10 Parser fixes** (S) — deps: T6
  - Accept: natural query text is what gets embedded (S3); "errors in python" keeps "python" (S4); "documents" is not a MIME filter (S6); "v2.0"/"node.js" produce no extension filter (S8); calendar-day dates in a configured timezone (S9).
  - Verify: table-driven `pytest` with one case per bug; `make eval` not worse.
- [ ] **T11 Reranker** (M) — deps: T7, T9
  - Accept: `/rerank` + `Rerank` RPC; Go returns results in reranker order (S2); absolute cut-off (S5); dedup by `object_key` (S7); response carries real per-leg scores or none (S10).
  - Verify: Go test with a stub worker proves order follows rerank scores; `make eval` improves or holds.
- [ ] **T12 Redis cache** (S) — deps: T7
  - Accept: key includes limit; empty/error responses not cached; search works with Redis down.
  - Verify: `go test ./cache/...` using miniredis.

### Checkpoint B
- [ ] Evaluation numbers recorded, ≥ Checkpoint A baseline
- [ ] Owner review

## Phase 3 — Images

- [ ] **T13 Image embedding + ingestion** (M) — deps: T9
  - Accept: `/embed/image`; `/embed/query` also returns `image_vector`; images indexed with `image_embedding` plus filename/EXIF text; resize matches the chosen model (I8).
  - Verify: `pytest` for model server and ingestion; uploading a JPEG creates one document with a vector of the spec's dimension.
- [ ] **T14 Image query leg** (M) — deps: T11, T13
  - Accept: third kNN leg with the same filters; text and image results combined by the documented rule; UI preview works.
  - Verify: `make eval` image queries meet the threshold set in T9; golden-JSON test for the three-leg query.

### Checkpoint C
- [ ] Text and image evaluation thresholds met
- [ ] Owner review
- [ ] `/coderabbit:code-review`

## Phase 4 — Ingestion reliability

- [ ] **T15 No silent drops** (S) — Accept: oversized → DLQ with reason (I1); no-text files indexed by filename/metadata (I9). Verify: unit tests for both paths.
- [ ] **T16 Ordered processing** (M) — Accept: an offset is committed only when every earlier offset on that partition is done (I2); download streamed, not blocking the loop (I6). Verify: test that kills processing mid-batch and asserts redelivery.
- [ ] **T17 Correct re-index and failure handling** (S) — Accept: re-upload with fewer chunks leaves no stale chunks (I3); any bulk item failure fails the message (I4); 4xx not retried (I7). Verify: unit tests against a fake OpenSearch.
- [ ] **T18 DLQ retry worker** (M) — Accept: retry count in a Kafka header; a message is retried at most N times in total; no self-consumption; no sleep longer than the poll interval (I5); service present in Compose. Verify: unit test simulating three consecutive failures ends in exactly one permanent-failure record.

### Checkpoint D
- [ ] Fault-injection checks pass (worker killed mid-file; shorter re-upload; Tika down)
- [ ] Owner review

## Phase 5 — Summary panel

- [ ] **T19 Extracted passage** (M) — deps: T11
  - Accept: top results carry the best-matching sentences; UI shows them highlighted; Flan-T5 is gone.
  - Verify: `pytest` for sentence selection; visual check in the UI.
- [ ] **T20 Generated answer (optional)** (M) — deps: T19
  - Accept: answer model loads only when enabled and CUDA is visible; `/answer` cites source filenames; UI requests it after results and hides the panel when unavailable; results are never delayed by it.
  - Verify: tests with the feature on (stubbed model) and off; manual check on GPU.

### Checkpoint E
- [ ] Works with answer model on and off
- [ ] Owner review
- [ ] `/coderabbit:code-review`

## Phase 6 — minikube

- [ ] **T21 Manifests + deploy script** (M) — deps: Checkpoint E; owner has installed minikube + kubectl
  - Accept: same services as Compose; credentials in a Secret; no unconditional GPU request; probes hit cheap endpoints; total memory requests fit in an 8 GB minikube.
  - Verify: `infrastructure/k8s/deploy.sh` then `make smoke TARGET=minikube`.
- [ ] **T22 GPU opt-in** (S) — deps: T21; owner has installed NVIDIA Container Toolkit
  - Accept: one flag enables GPU for the model server on Compose and on minikube; without it everything runs on CPU.
  - Verify: `/health` reports `cuda` with the flag and `cpu` without.

### Checkpoint F
- [ ] Smoke test green on minikube
- [ ] Owner review

## Phase 7 — Finish

- [ ] **T23 Docs** (S) — Accept: `docs/architecture.md`, `docs/setup.md`, README match the code; measured memory per service recorded. Verify: a fresh clone following `setup.md` reaches a green smoke test.
- [ ] **T24 Final pass** (S) — Accept: model names/dimensions appear only in `model_spec.json`; no unused files or dependencies; all tests green. Verify: `grep` for model names; `make test`.

### Checkpoint G
- [ ] Every success criterion in `docs/intent/refactor.md` met
- [ ] `/coderabbit:code-review`
- [ ] Owner sign-off
