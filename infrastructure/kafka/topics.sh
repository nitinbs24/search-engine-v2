#!/bin/bash
# Creates the upload-event topic, its dead-letter topic and the permanent-failure topic. Safe to re-run.
set -euo pipefail
for topic in "$KAFKA_TOPIC" "$KAFKA_DLQ_TOPIC" "$KAFKA_DLQ_FAILED_TOPIC"; do
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --create --if-not-exists --topic "$topic" --partitions 3 --replication-factor 1
done
