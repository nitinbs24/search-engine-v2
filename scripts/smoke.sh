#!/bin/bash
# End-to-end check against a running stack (make up): upload a file under a prefix,
# wait until the UI's search route finds it, download it back and compare the bytes,
# then delete it and wait until it is gone from the results.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

UI=http://localhost:3000
token="smoke$(date +%s)"
key="smoke/$token.txt"
body="Smoke test document $token. The zeppelin navigator charted a careful course over the glacier before nightfall."

mc() {
  docker compose --env-file .env -f infrastructure/docker-compose.yml exec -T minio sh -c \
    'export MC_CONFIG_DIR=/tmp/.mc; mc alias set l http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc "$@"' mc "$@"
}

printf '%s' "$body" | mc pipe --attr Content-Type=text/plain "l/$MINIO_BUCKET/$key" >/dev/null
trap 'mc rm "l/$MINIO_BUCKET/$key" >/dev/null 2>&1 || true' EXIT

found=
for _ in $(seq 30); do
  if curl -fsS "$UI/api/search?q=zeppelin+navigator+$token" | grep -q "\"object_key\":\"$key\""; then
    found=1
    break
  fi
  sleep 2
done
[ -n "$found" ] || { echo "FAIL: $key was not found through $UI/api/search within 60s"; exit 1; }

got=$(curl -fsS "$UI/api/download?bucket=$MINIO_BUCKET&key=$key")
[ "$got" = "$body" ] || { echo "FAIL: downloaded bytes differ from the upload"; exit 1; }

mc rm "l/$MINIO_BUCKET/$key" >/dev/null
gone=
for _ in $(seq 30); do
  if ! curl -fsS "$UI/api/search?q=zeppelin+navigator+$token" | grep -q "\"object_key\":\"$key\""; then
    gone=1
    break
  fi
  sleep 2
done
[ -n "$gone" ] || { echo "FAIL: $key is still in the search results 60s after it was deleted"; exit 1; }

echo "smoke test passed: uploaded, found, downloaded and deleted $key"
