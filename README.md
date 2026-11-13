# search-engine-v2

Self-hosted natural-language search over files in an object store. Drop documents or images into
MinIO; ask for them in plain English ("pdfs about bread uploaded last week", "a dog on a beach").
Results come back reranked, with the best sentence highlighted and an optional generated answer.

Small models (a few hundred MB in total) on CPU, or on a 4 GB GPU when available. Fits a 15 GB laptop.

```bash
make up && make smoke                                          # Docker Compose
bash infrastructure/k8s/deploy.sh && make smoke TARGET=minikube   # minikube
```

UI: http://localhost:3000 · upload through the MinIO console at http://localhost:9001.
Add `GPU=1` to either command for the GPU.

| | |
|---|---|
| Stack | MinIO · Kafka · Tika · OpenSearch · Redis · Go gateway · Python gRPC worker · FastAPI model server · Next.js |
| Setup, configuration, tests | [docs/setup.md](docs/setup.md) |
| How it works, memory per service | [docs/architecture.md](docs/architecture.md) |
| Model choice and search quality | [docs/model-evaluation.md](docs/model-evaluation.md) |
| Bugs fixed from the original repo | [docs/ANALYSIS_REPORT.md](docs/ANALYSIS_REPORT.md) |

`make test` runs every unit test (Python, Go, frontend, k8s manifests) in containers.
