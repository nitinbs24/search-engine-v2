# Build context is the repo root: the proto lives in shared/proto.
FROM python:3.11-slim
WORKDIR /app
COPY workers/search/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/proto/search.proto /proto/
RUN mkdir proto_gen && python -m grpc_tools.protoc -I /proto --python_out=proto_gen --grpc_python_out=proto_gen search.proto
ENV PYTHONPATH=/app/proto_gen
COPY workers/search/*.py ./
EXPOSE 50051
CMD ["python", "main.py"]
