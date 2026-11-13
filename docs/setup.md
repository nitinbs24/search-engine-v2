# Setup

Tested on Fedora with a 12-thread CPU, 15 GB RAM and a GTX 1650 Ti (4 GB), Docker Engine 29.

## Prerequisites

| Need | For |
|---|---|
| Docker Engine with Compose v2 | everything (Go, Node and Python tooling run in containers) |
| ~15 GB free disk for images, models and data | everything |
| `make`, `bash`, `curl` | the make targets and scripts |
| minikube + kubectl | the minikube target only |
| NVIDIA driver + NVIDIA Container Toolkit | `GPU=1` only |

minikube and kubectl install without root:

```bash
curl -Lo ~/.local/bin/kubectl "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
curl -Lo ~/.local/bin/minikube https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
chmod +x ~/.local/bin/kubectl ~/.local/bin/minikube
```

## Configuration

`.env` is created from `.env.example` on first `make up` (or `deploy.sh`). The defaults are
local-development credentials; change them before exposing anything. Optional settings:

| Variable | Default | Effect |
|---|---|---|
| `ANSWER_ENABLED` | `0` | `1` loads the answer LLM, only if a CUDA device is visible (needs `GPU=1`). |
| `RERANK_MIN_SCORE` | -7.0 | Drop text hits whose reranker score is below this. |
| `IMAGE_MIN_SCORE` | 0.24 | Drop image hits whose cosine similarity is below this. |

## Docker Compose

```bash
make up        # build, start, wait until healthy (first run: ~20–40 min, mostly downloads)
make smoke     # upload a file, find it via the UI's API, download it, delete it
```

Then open http://localhost:3000. Upload files through the MinIO console at http://localhost:9001
(user and password from `.env`, bucket `uploads`); they are searchable a few seconds later.

`make down` stops the stack and keeps the data volumes. `make up GPU=1` runs the model server on
the GPU (check with `curl localhost:8000/health`, which reports `"device":"cuda"`).

## minikube

```bash
bash infrastructure/k8s/deploy.sh   # starts minikube (8 GB, 4 CPUs) if needed, builds, deploys
make smoke TARGET=minikube
kubectl -n search-engine port-forward svc/frontend 3000   # UI on http://localhost:3000
```

The first deploy takes ~25–45 min (base image, image builds, model download); re-deploys ~6 min.
`deploy.sh` is safe to re-run and restarts pods so they pick up rebuilt images.
`minikube stop` frees the memory and keeps the cluster.

GPU: the cluster must be created with one, so `minikube delete` an existing cluster first, then
`GPU=1 bash infrastructure/k8s/deploy.sh`.

## Tests

| Command | Needs | Runs |
|---|---|---|
| `make test` | Docker | Python unit tests per worker, frontend lib tests, `go vet` + `go test`, k8s manifest checks |
| `make smoke` | running stack | upload → search → download → delete |
| `make integration` | running Compose stack | end-to-end cases in `tests/integration` |
| `make eval` | running Compose stack, local `eval/` corpus | recall@5 and MRR (see `docs/model-evaluation.md`) |

`eval/` (corpus, queries, runner) is kept out of git, so `make eval` only works where it exists.

## Troubleshooting

- **A setup Job never completes on minikube:** `kubectl -n search-engine logs job/<name>`.
  `deploy.sh` stops at the first Job that fails.
- **`kubectl` says `localhost:8080 refused`:** no cluster yet; `minikube start` (or `deploy.sh`)
  writes `~/.kube/config` once the cluster is up.
- **OpenSearch exits at start:** the host needs `vm.max_map_count` ≥ 262144
  (`sudo sysctl -w vm.max_map_count=262144`).
- **The model server takes minutes to become ready:** the first start downloads the models into
  the `hf-cache` volume; later starts load from it.
