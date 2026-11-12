# Refactor — Statement of Intent

Confirmed by the owner on 2026-11-11. Companion to `docs/ANALYSIS_REPORT.md`.

## Outcome
A **new repository** in the planned "Obsidian" layout, built by carrying over the code worth keeping from this repo and leaving dead and duplicate code behind. This repo stays untouched as the reference.

```
infrastructure/   minio/ kafka/ redis/ opensearch/ k8s/ docker-compose.yml
workers/
  model-server/   FastAPI — the only place models are loaded
  ingestion/      Kafka consumer → Tika → chunk → model-server → OpenSearch
  search/         gRPC — query parsing + query vector only
backend/          Go — routes, Redis cache, hybrid OpenSearch query, gRPC client
frontend/         Next.js (JavaScript), current chat-style UI
shared/proto/     single search.proto
docs/
```

## User
The owner, working solo. Role-ownership notes are dropped.

## Success
- Text search and image search both return relevant results on small models.
- One command brings the stack up on Docker Compose; one on minikube.
- Fits a 15 GB RAM laptop, on CPU or on GPU (GTX 1650 Ti, 4 GB) when available.
- Bugs listed in the analysis report are fixed, with tests on the query parser, the Go query builder and the gateway.

## Constraint
Same tech stack: MinIO, Kafka, Tika, OpenSearch, Redis, Go gateway, gRPC Python worker, Next.js.

## Design decisions
- One model server holds the text embedder, image embedder, reranker and the optional answer LLM. Uses the GPU when visible, otherwise CPU.
- Go builds and runs the hybrid OpenSearch query. The Python search worker only parses the query and returns the vector.
- Frontend keeps the current chat UI, in JavaScript; download bug fixed.
- Summary panel is layered: query-focused extracted passage always; generated answer from a small instruct LLM is opt-in, GPU only, fetched separately from results.
- Kafka: single broker locally.
- Index starts clean (model and vector dimension change).
- Kubernetes manifests live under `infrastructure/k8s/`.
- Docker is the target runtime.

## Process
- Test-driven (`/agent-skills:test`).
- CodeRabbit review (`/coderabbit:code-review`) after every few phases.

## Out of scope
Role-based access and login, upload UI, filter sidebar, TypeScript conversion, Nginx load balancer for model-server replicas, MongoDB, CI, podman.

## Still open
- New repo location, name, and whether git history is fresh or carried over.
- Exact models — to be chosen by testing candidates on real documents.
- NVIDIA Container Toolkit is not installed; needed before containers can use the GPU.
