#!/bin/sh
# Creates the upload bucket and sends its object-created events to Kafka. Safe to re-run.
set -eu
mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "local/$MINIO_BUCKET"
mc event add --ignore-existing "local/$MINIO_BUCKET" arn:minio:sqs::1:kafka --event put
mc event list "local/$MINIO_BUCKET"
