#!/bin/bash
# Creates the upload-event topic and its dead-letter topic. Safe to re-run.
set -euo pipefail
for topic in "$KAFKA_TOPIC" "$KAFKA_DLQ_TOPIC"; do
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --create --if-not-exists --topic "$topic" --partitions 3 --replication-factor 1
done
