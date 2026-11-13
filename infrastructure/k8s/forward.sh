#!/usr/bin/env bash
# Forward the UI, MinIO console and OpenSearch API to localhost. Ctrl-C stops all of them.
# Run after deploy.sh. Forwards drop if a pod restarts; just re-run.
set -euo pipefail

NS=search-engine
trap 'kill 0' EXIT INT TERM

for spec in frontend:3000 minio:9001 opensearch:9200 opensearch-dashboards:5601; do
  kubectl -n "$NS" port-forward "svc/${spec%%:*}" "${spec##*:}" >/dev/null &
done

echo "UI          http://localhost:3000"
echo "MinIO       http://localhost:9001"
echo "OpenSearch  http://localhost:9200"
echo "Dashboards  http://localhost:5601"
echo "Ctrl-C to stop."
wait
