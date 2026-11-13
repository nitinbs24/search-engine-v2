#!/bin/bash
# Deploys the stack to minikube. Safe to re-run.
#   bash infrastructure/k8s/deploy.sh          CPU only
#   GPU=1 bash infrastructure/k8s/deploy.sh    model server gets the GPU (NVIDIA Container Toolkit;
#                                              the minikube cluster must have been created with GPU=1)
# Images are built with the Compose build straight into minikube's Docker; nothing is pushed.
# Services are ClusterIP only. Open the UI with: kubectl -n search-engine port-forward svc/frontend 3000
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] || cp .env.example .env
NS=search-engine
GPU=${GPU:-0}
K8S=infrastructure/k8s
k() { kubectl -n "$NS" "$@"; }
generated() { "$@" --dry-run=client -o yaml | k apply -f -; }
# A rebuilt image keeps its tag, so pods that already exist only pick it up on restart.
restart_existing() { for d; do if grep -qx "deployment.apps/$d" <<<"$existing"; then k rollout restart "deploy/$d"; fi; done; }
run_job() {  # Jobs are immutable: replace, then wait for completion
  k delete job "$1" --ignore-not-found
  k apply -f $K8S/jobs.yaml -l job="$1"
  k wait --for=condition=complete "job/$1" --timeout=5m
}

if ! minikube status >/dev/null 2>&1; then
  gpu_flag=()
  if [ "$GPU" = 1 ]; then gpu_flag=(--gpus=all); fi
  minikube start --driver=docker --container-runtime=docker --memory=8g --cpus=4 "${gpu_flag[@]}"
fi
if [ "$GPU" = 1 ]; then
  gpus=
  for _ in $(seq 30); do  # the device plugin registers a few seconds after start
    gpus=$(kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}')
    [ -n "$gpus" ] && break
    sleep 2
  done
  [ -n "$gpus" ] || { echo "minikube has no GPU. Run 'minikube delete', then rerun with GPU=1." >&2; exit 1; }
fi

compose=(docker compose --env-file .env -f infrastructure/docker-compose.yml)
if [ "$GPU" = 1 ]; then compose+=(-f infrastructure/docker-compose.gpu.yml); fi
(eval "$(minikube docker-env)" && "${compose[@]}" build model-server ingestion search-worker backend frontend)

existing=$(k get deploy -o name 2>/dev/null || true)
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
# .env over .env.example: keys added to the example after .env was made still get their default
generated k create secret generic app-env --from-env-file=<(cat .env.example .env |
  awk -F= '/^[A-Z_]+=/ {v[$1] = $0} END {for (k in v) print v[k]}')
generated k create configmap kafka-topics --from-file=infrastructure/kafka/topics.sh
generated k create configmap minio-setup --from-file=infrastructure/minio/setup.sh
generated k create configmap opensearch-init --from-file=infrastructure/opensearch/init.py \
  --from-file=workers/model-server/model_spec.json
generated k create configmap redis-conf --from-file=infrastructure/redis/redis.conf

# Same order as the Compose depends_on chain.
k apply -f $K8S/backing.yaml -l 'app!=minio'
k rollout status deploy/kafka --timeout=5m
run_job kafka-init
k apply -f $K8S/backing.yaml -l app=minio  # its Kafka notification target needs the topics
k rollout status deploy/minio --timeout=5m
run_job minio-init
k rollout status deploy/opensearch --timeout=10m
run_job opensearch-init

k apply -f $K8S/apps.yaml -l app=model-server
if [ "$GPU" = 1 ]; then
  k patch deploy/model-server -p '{"spec":{"template":{"spec":{"containers":[{"name":"model-server","resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}}}'
else  # apply never removes a patched field, so a GPU from an earlier run is taken off here
  k patch deploy/model-server --type json \
    -p '[{"op":"remove","path":"/spec/template/spec/containers/0/resources/limits/nvidia.com~1gpu"}]' 2>/dev/null || true
fi
restart_existing model-server
k rollout status deploy/model-server --timeout=20m  # first start downloads the models
k apply -f $K8S/apps.yaml -l 'app!=model-server'
restart_existing ingestion dlq-retry search-worker backend frontend
for d in tika redis search-worker backend frontend ingestion dlq-retry; do
  k rollout status "deploy/$d" --timeout=5m
done
echo "Deployed to namespace $NS. Smoke test: make smoke TARGET=minikube"
